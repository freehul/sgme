"""T11 测试：信号引擎（publish / pull / replay / SSE 端点 / refine 集成）。

覆盖：
- publish：signal_events 表新增一行
- pull：返回事件 + next_cursor 推进
- pull：订阅者游标持久化
- pull：last_signal_id 覆盖持久游标
- publish：同源同类型 30 分钟内重复 → payload 含 suppress_hint
- replay：超 1h 事件合并为 _summary
- refine_file 成功后 signal_events 出现 memory_updated（mock LLM）
- SSE 端点连接后推送事件（TestClient）
- /v1/events/pull 端点
- /v1/events/stream 端点可连接
- 无 Key 调 events 端点 → 403
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from anyio import move_on_after
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import l1, refine as refine_mod
from sgme.raw import store as raw_store
from sgme.server.app import create_app
from sgme.signal import engine as signal_engine
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao, signal_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def app(tmp_path, cfg, raw_dir, monkeypatch):
    """创建隔离的 FastAPI 应用。"""
    # 清理可能被其他测试设置的环境变量，避免 Bearer 泄漏
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer，避免环境变量泄漏
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


AGENT_HEADERS = {"X-API-Key": "test-agent-key"}
ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_minus_minutes(minutes: int) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 1. publish 创建事件 ----------

def test_publish_creates_event(mem_conn):
    """publish 后 signal_events 表出现一行。"""
    # Arrange
    payload = {"file_id": "f1", "memories_count": 3}

    # Act
    event_id = signal_engine.publish(
        event_type="memory_updated",
        source="refine",
        payload=payload,
        mem_conn=mem_conn,
    )

    # Assert
    e = signal_dao.get_event(mem_conn, event_id)
    assert e is not None
    assert e["type"] == "memory_updated"
    assert e["source"] == "refine"
    body = json.loads(e["payload"])
    assert body["file_id"] == "f1"
    assert body["memories_count"] == 3
    assert e["consumed_at"] is None


# ---------- 2. pull 返回事件并推进游标 ----------

def test_pull_returns_events_and_advances_cursor(mem_conn):
    """pull 返回事件 + next_cursor 推进。"""
    # Arrange：间隔 > 1s 确保不同 ts（UUIDv4 非时序，靠 ts 排序）
    signal_engine.publish("memory_updated", "refine", {"n": 1}, mem_conn)
    time.sleep(1.1)
    signal_engine.publish("memory_updated", "refine", {"n": 2}, mem_conn)

    # Act
    result = signal_engine.pull(mem_conn, subscriber_id="sub1", limit=10)

    # Assert
    assert len(result["events"]) == 2
    assert result["next_cursor"] is not None
    # 第二次拉取无新事件 → next_cursor 为 None
    r2 = signal_engine.pull(mem_conn, subscriber_id="sub1", limit=10)
    assert r2["events"] == []
    assert r2["next_cursor"] is None


# ---------- 3. pull 订阅者游标持久化 ----------

def test_pull_with_subscriber_id_persists_cursor(mem_conn):
    """pull 后订阅者游标持久化在 signal_subscribers 表。"""
    # Arrange：间隔 > 1s 确保不同 ts
    signal_engine.publish("memory_updated", "refine", {"n": 1}, mem_conn)
    time.sleep(1.1)
    signal_engine.publish("memory_updated", "refine", {"n": 2}, mem_conn)

    # Act
    signal_engine.pull(mem_conn, subscriber_id="persist-sub", limit=1)

    # Assert
    sub = signal_dao.get_subscriber(mem_conn, "persist-sub")
    assert sub is not None
    # 只拉取 1 条，游标应推进到第一条 event_id
    assert sub["last_signal_id"] is not None
    # 再拉一次，应有 1 条剩余
    r2 = signal_engine.pull(mem_conn, subscriber_id="persist-sub", limit=10)
    assert len(r2["events"]) == 1


# ---------- 4. last_signal_id 覆盖持久游标 ----------

def test_pull_with_last_signal_id_override(mem_conn):
    """last_signal_id 参数覆盖订阅者持久游标。"""
    # Arrange：间隔 > 1s 发布 3 条事件，确保 ts 严格递增
    e1 = signal_engine.publish("memory_updated", "refine", {"n": 1}, mem_conn)
    time.sleep(1.1)
    signal_engine.publish("memory_updated", "refine", {"n": 2}, mem_conn)
    time.sleep(1.1)
    signal_engine.publish("memory_updated", "refine", {"n": 3}, mem_conn)

    # 先把订阅者游标拉到末尾
    signal_engine.pull(mem_conn, subscriber_id="override-sub", limit=100)
    sub = signal_dao.get_subscriber(mem_conn, "override-sub")
    assert sub["last_signal_id"] is not None

    # Act：传 last_signal_id=e1，覆盖游标到 e1 之后
    result = signal_engine.pull(
        mem_conn, subscriber_id="override-sub", last_signal_id=e1, limit=100
    )

    # Assert：返回 e1 之后的事件（2 条）
    assert len(result["events"]) == 2
    assert result["events"][0]["payload"]["n"] == 2
    assert result["events"][1]["payload"]["n"] == 3


# ---------- 5. suppress_hint 包含同源同类型 30 分钟内重复 ----------

def test_suppress_hint_included(mem_conn):
    """同源同类型 30 分钟内重复 publish，payload 含 suppress_hint。"""
    # Arrange：先发布一次
    eid1 = signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"k": "v1"}, mem_conn=mem_conn,
    )
    e1 = signal_dao.get_event(mem_conn, eid1)
    e1_ts = e1["ts"]

    # Act：紧接着再发布一次（30 分钟内）
    eid2 = signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"k": "v2"}, mem_conn=mem_conn,
    )

    # Assert
    e2 = signal_dao.get_event(mem_conn, eid2)
    body = json.loads(e2["payload"])
    assert "suppress_hint" in body
    assert body["suppress_hint"] == e1_ts
    assert body["k"] == "v2"


def test_suppress_hint_not_included_beyond_window(mem_conn):
    """同源同类型但超过 30 分钟 → 不附 suppress_hint。"""
    # Arrange：手工插入一条 1 小时前的事件
    old_ts = _iso_minus_minutes(60)
    signal_dao.insert_event(
        mem_conn, str(uuid.uuid4()),
        "memory_updated", "refine",
        json.dumps({"k": "v0"}), old_ts,
    )

    # Act：再发布一次
    eid = signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"k": "v1"}, mem_conn=mem_conn,
    )

    # Assert
    e = signal_dao.get_event(mem_conn, eid)
    body = json.loads(e["payload"])
    assert "suppress_hint" not in body


# ---------- 6. replay window 合并超窗口事件 ----------

def test_replay_window_merges_old_events(mem_conn):
    """超 1h 事件合并为 _summary。"""
    # Arrange：插入 1 条 2 小时前事件 + 1 条当前事件
    old_ts = _iso_minus_minutes(120)
    signal_dao.insert_event(
        mem_conn, str(uuid.uuid4()),
        "memory_updated", "refine",
        json.dumps({"old": True}), old_ts,
    )
    signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"fresh": True}, mem_conn=mem_conn,
    )

    # Act
    events = signal_engine.get_replay_window_events(mem_conn, since_ts=old_ts)

    # Assert：首条是 _summary，后面是窗口内事件
    assert events[0]["type"] == "_summary"
    assert events[0]["payload"]["old_events_count"] == 1
    assert any(e["payload"].get("fresh") is True for e in events[1:])


def test_replay_window_no_summary_when_all_in_window(mem_conn):
    """全部事件在窗口内 → 无 _summary。"""
    signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"fresh": True}, mem_conn=mem_conn,
    )
    events = signal_engine.get_replay_window_events(
        mem_conn, since_ts=_iso_minus_minutes(120)
    )
    assert all(e["type"] != "_summary" for e in events)
    assert len(events) == 1


# ---------- 7. refine_file 成功后发布 memory_updated ----------

def test_refine_publishes_memory_updated(mem_conn, session_conn, cfg, raw_dir, monkeypatch):
    """refine_file 成功后 signal_events 出现 memory_updated（mock LLM）。"""
    # Arrange：构造 raw 文件 + raw_files 行
    fid = "f-signal-refine"
    msgs = [
        {"timestamp": "2026-08-04T10:00:00Z", "role": "user",
         "content": "我用 Python 写后端"},
        {"timestamp": "2026-08-04T10:01:00Z", "role": "assistant",
         "content": "好的，记下了"},
    ]
    raw_store.write_new_file(
        file_id=fid, session_key="sess_signal", started_at="2026-08-04T10:00:00Z",
        agent_id="test", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=raw_store.relative_path(fid),
        session_key="sess_signal", started_at="2026-08-04T10:00:00Z",
        agent_id="test", status="new", size=raw_store.file_size(fid),
    )

    # mock L1 + L2（L2 用空场景避免触发聚合）
    l1_body = json.dumps([
        {"content": "用户用 Python 写后端", "dimensions": ["技术栈"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static",
         "source_message_ids": [1]},
    ])
    l2_body = json.dumps([])  # 空动作列表

    state = {"i": 0}

    def handler(req):
        i = state["i"]
        state["i"] = i + 1
        body = l1_body if i == 0 else l2_body
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body}}]
        })

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    # Act：refine_file + 模拟 L1.5 落库 + finalize_refinement（L2 空动作 + 信号）
    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.status == "refined"
    for m in result.memories:
        mid = memory_dao.insert_memory(
            mem_conn,
            content=m["content"],
            memory_type=m.get("memory_type", "persona"),
            priority=m.get("priority", 50),
            time_velocity=m.get("time_velocity", "static"),
            ttl_days=None,
            dimension_ids=m.get("dimension_ids", []),
            sources=[(fid, "session")],
        )
        m["memory_id"] = mid
    refine_mod.finalize_refinement(result, mem_conn, cfg, client=cli)

    # Assert
    assert result.status == "refined"
    # 查 signal_events：应有 source='refine' 的事件
    cur = mem_conn.execute(
        "SELECT * FROM signal_events WHERE source='refine' AND type='memory_updated'"
    )
    rows = [dict(r) for r in cur.fetchall()]
    assert len(rows) >= 1
    body = json.loads(rows[0]["payload"])
    assert body["file_id"] == fid
    assert body["memories_count"] == 1


# ---------- 8. SSE 端点推送事件 ----------

@pytest.mark.asyncio
async def test_sse_stream_pushes_events(app):
    """SSE 端点连接后推送事件（直接调用端点函数 + body_iterator 迭代）。"""
    # Arrange：发布一条事件
    mem_conn = app.state.mem_conn
    signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"k": "v"}, mem_conn=mem_conn,
    )

    # Act：直接调用端点函数获取 StreamingResponse，迭代 body_iterator
    from sgme.server.routes_events import event_stream

    class MockRequest:
        def __init__(self, app_state):
            self.app = type("App", (), {"state": app_state})()
            self.headers = {}
            self._disconnected = False

        async def is_disconnected(self):
            return self._disconnected

    request = MockRequest(app.state)
    response = await event_stream(request, subscriber_id="sse-test", _="key")

    assert response.media_type == "text/event-stream"

    received = ""
    gen = response.body_iterator
    try:
        with move_on_after(5):
            async for chunk in gen:
                received += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                if "data:" in received and received.endswith("\n\n"):
                    break
    finally:
        await gen.aclose()

    # Assert
    assert "id:" in received
    assert "event: memory_updated" in received
    assert "data:" in received


# ---------- 9. /v1/events/pull 端点 ----------

def test_events_pull_endpoint(client):
    """/v1/events/pull 端点测试。"""
    # Arrange：发布事件
    app = client.app
    mem_conn = app.state.mem_conn
    signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"hello": "world"}, mem_conn=mem_conn,
    )

    # Act
    resp = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "endpoint-sub", "limit": 10},
        headers=AGENT_HEADERS,
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "events" in body
    assert "next_cursor" in body
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "memory_updated"
    assert body["events"][0]["payload"]["hello"] == "world"
    assert body["next_cursor"] is not None


# ---------- 10. /v1/events/stream 端点可连接 ----------

@pytest.mark.asyncio
async def test_events_stream_endpoint(app):
    """/v1/events/stream 端点可连接（有事件时推送事件块）。"""
    # Arrange：先发布事件，确保首块快速到达
    mem_conn = app.state.mem_conn
    signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"hello": "stream"}, mem_conn=mem_conn,
    )

    # Act：直接调用端点函数
    from sgme.server.routes_events import event_stream

    class MockRequest:
        def __init__(self, app_state):
            self.app = type("App", (), {"state": app_state})()
            self.headers = {}
            self._disconnected = False

        async def is_disconnected(self):
            return self._disconnected

    request = MockRequest(app.state)
    response = await event_stream(request, subscriber_id="sse-conn-test", _="key")

    assert response.media_type == "text/event-stream"

    first_chunk = ""
    gen = response.body_iterator
    try:
        with move_on_after(5):
            async for chunk in gen:
                first_chunk += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                if first_chunk.endswith("\n\n"):
                    break
    finally:
        await gen.aclose()

    # Assert：首块含 SSE 标记（事件 id: 或 keepalive :）
    assert first_chunk
    assert first_chunk.startswith("id:") or first_chunk.startswith(":")


# ---------- 11. 无 Key 调 events 端点 → 403 ----------

def test_no_key_events_pull_endpoint_403(client):
    """无 Key 调 /v1/events/pull → 403。"""
    resp = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "no-key-sub"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_no_key_events_stream_endpoint_403(client):
    """无 Key 调 /v1/events/stream → 403。"""
    with client.stream(
        "GET", "/v1/events/stream", timeout=5,
    ) as resp:
        assert resp.status_code == 403
        assert resp.headers["content-type"].startswith("application/json")
