"""tests/test_operations_events.py：operations 层 events 链路测试（v0.8 T-8）。

覆盖（照 tests/test_operations_health.py 的 fixture 范式）：
1. operations.events_pull() 返回 OperationResult(ok=True)，data 为协议无关超集
2. operations.events_list() 返回 OperationResult(ok=True)，固定契约兼容订阅者
3. events_stream 生成器：yield 事件信封 / keepalive 哨兵 / 断连退出 /
   Last-Event-ID 重连补偿在调用时立即生效
4. http_payload 恒等投影（HTTP 形态即超集本身）
5. **契约等价性**（最关键）：改造后三个端点响应 vs 既有测试冻结断言
   （tests/test_signal.py + tests/test_server_v04.py，v0.4 逐字段抄录）逐条一致，
   SSE 帧格式逐字节不变
6. 鉴权契约不变：无 Key → 403 ERR_FORBIDDEN（test_signal 冻结）
7. 同一库状态下 operations 直调结果与 HTTP 端点响应逐字段一致
"""
from __future__ import annotations

import json

import pytest
from anyio import move_on_after
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, signal_dao
from sgme.operations.errors import OperationResult
from sgme.operations.events import (
    CONTRACT_LEGACY_SUBSCRIBER,
    KEEPALIVE_MARK,
    events_list,
    events_pull,
    events_stream,
    http_payload,
)
from sgme.server.app import create_app
from sgme.signal import engine as signal_engine

# ---------- v0.4 冻结契约（改造前 test_signal.py / test_server_v04.py 逐字段抄录） ----------

# pull/list 响应顶层字段（test_signal.py::test_events_pull_endpoint 冻结）
PULL_RESPONSE_KEYS = ("events", "next_cursor")
# 事件信封字段（signal_engine.pull 归一化输出，SSE data 行同构）
EVENT_ENVELOPE_KEYS = ("event_id", "type", "source", "payload", "ts")
# SSE 响应媒体类型（test_signal.py::test_sse_stream_pushes_events 冻结）
SSE_MEDIA_TYPE = "text/event-stream"
# SSE 帧必备标记（id: / event: / data:，test_signal 冻结）
SSE_FRAME_MARKERS = ("id:", "event:", "data:")


# ---------- fixtures（照 test_operations_health.py 范式） ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def mem_conn(conns):
    return conns[0]


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（复用同一批连接，便于与 operations 直调对照）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


# ---------- 工具 ----------

def _publish(mem_conn, payload: dict | None = None, source: str = "refine") -> str:
    """发布一条 memory_updated 事件，返回 event_id。"""
    return signal_engine.publish(
        event_type="memory_updated",
        source=source,
        payload=payload if payload is not None else {"k": "v"},
        mem_conn=mem_conn,
    )


# ---------- 1. 操作函数返回 OperationResult ----------

def test_events_pull_returns_operation_result_ok(mem_conn):
    """events_pull 返回 OperationResult(ok=True)，data 为协议无关超集。"""
    # Arrange
    _publish(mem_conn, {"hello": "world"})

    # Act
    res = events_pull(mem_conn, "sub-op", None, 10)

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert list(res.data.keys()) == list(PULL_RESPONSE_KEYS)
    assert len(res.data["events"]) == 1
    assert list(res.data["events"][0].keys()) == list(EVENT_ENVELOPE_KEYS)
    assert res.data["events"][0]["type"] == "memory_updated"
    assert res.data["events"][0]["payload"] == {"hello": "world"}
    assert res.data["next_cursor"] is not None


def test_events_pull_empty_returns_ok(mem_conn):
    """无事件 → ok=True，events 为空列表，next_cursor=None。"""
    # Act
    res = events_pull(mem_conn, "sub-empty", None, 10)

    # Assert
    assert res.ok is True
    assert res.data == {"events": [], "next_cursor": None}


def test_events_pull_last_signal_id_overrides_cursor(mem_conn):
    """last_signal_id 覆盖持久游标（改造前 pull 语义保留）。"""
    # Arrange：间隔 > 1s 确保 ts 严格递增（UUIDv4 非时序，靠 ts 排序）
    import time
    e1 = _publish(mem_conn, {"n": 1})
    time.sleep(1.1)
    e2 = _publish(mem_conn, {"n": 2})
    events_pull(mem_conn, "sub-ov", None, 100)  # 先把持久游标推到末尾

    # Act：传 last_signal_id=e1 覆盖游标
    res = events_pull(mem_conn, "sub-ov", e1, 100)

    # Assert：只返回 e1 之后的事件，next_cursor 为 e2
    assert [e["payload"]["n"] for e in res.data["events"]] == [2]
    assert res.data["next_cursor"] == e2


def test_events_list_returns_operation_result_ok(mem_conn):
    """events_list 返回 OperationResult(ok=True)，固定契约兼容订阅者。"""
    # Arrange
    _publish(mem_conn, {"k": "v"})

    # Act
    res = events_list(mem_conn, None, 10)

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert list(res.data.keys()) == list(PULL_RESPONSE_KEYS)
    assert len(res.data["events"]) == 1
    # 订阅者游标落在契约兼容订阅者名下，且与 next_cursor 一致
    sub = signal_dao.get_subscriber(mem_conn, CONTRACT_LEGACY_SUBSCRIBER)
    assert sub is not None
    assert sub["last_signal_id"] == res.data["next_cursor"]


def test_events_list_after_overrides_cursor(mem_conn):
    """after 游标覆盖持久游标（契约 §4.5 语义）。"""
    # Arrange
    import time
    e1 = _publish(mem_conn, {"n": 1})
    time.sleep(1.1)
    _publish(mem_conn, {"n": 2})

    # Act
    res = events_list(mem_conn, after=e1, limit=10)

    # Assert
    assert [e["payload"]["n"] for e in res.data["events"]] == [2]


# ---------- 2. HTTP 形态字段完整 ----------

def test_events_pull_http_shape_complete(client, app):
    """GET /v1/events/pull 响应字段集合完整（HTTP 历史形态）。"""
    # Arrange
    _publish(app.state.mem_conn, {"hello": "world"})

    # Act
    resp = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "http-sub", "limit": 10},
        headers=AGENT_HEADERS,
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == list(PULL_RESPONSE_KEYS)
    assert len(body["events"]) == 1
    assert list(body["events"][0].keys()) == list(EVENT_ENVELOPE_KEYS)
    assert body["events"][0]["type"] == "memory_updated"
    assert body["events"][0]["payload"] == {"hello": "world"}
    assert body["next_cursor"] is not None


def test_events_after_http_shape_complete(client, app):
    """GET /v1/events?after= 响应字段集合完整（契约 §4.5 兼容路径）。"""
    # Arrange
    _publish(app.state.mem_conn, {"k": "v"})

    # Act（after="" 空串归一为 None，test_server_v04 冻结的调用形态）
    resp = client.get(
        "/v1/events", params={"after": "", "limit": 10}, headers=AGENT_HEADERS
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == list(PULL_RESPONSE_KEYS)
    assert len(body["events"]) >= 1
    assert list(body["events"][0].keys()) == list(EVENT_ENVELOPE_KEYS)


# ---------- 3. events_stream 生成器（操作层，协议无关） ----------

@pytest.mark.asyncio
async def test_events_stream_generator_yields_event_envelopes(mem_conn, monkeypatch):
    """events_stream yield 事件信封 dict（协议无关，非 SSE 帧）。"""
    # Arrange：缩短轮询间隔加速测试；首轮拉取后断开
    monkeypatch.setattr("sgme.operations.events.SSE_POLL_INTERVAL_SEC", 0.01)
    _publish(mem_conn, {"n": 1})
    calls = {"n": 0}

    async def disconnected():
        calls["n"] += 1
        return calls["n"] >= 2

    # Act
    stream = events_stream(mem_conn, "stream-op", is_disconnected=disconnected)
    got = [item async for item in stream]

    # Assert
    assert len(got) == 1
    assert list(got[0].keys()) == list(EVENT_ENVELOPE_KEYS)
    assert got[0]["type"] == "memory_updated"
    assert got[0]["payload"] == {"n": 1}


@pytest.mark.asyncio
async def test_events_stream_generator_keepalive_mark(mem_conn, monkeypatch):
    """无事件轮询累计达到 keepalive 周期 → yield keepalive 哨兵。"""
    # Arrange：轮询 0.01s，keepalive 周期 0.03s → 第 4 轮（counter=3）触发
    monkeypatch.setattr("sgme.operations.events.SSE_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr("sgme.operations.events.SSE_KEEPALIVE_INTERVAL_SEC", 0.03)
    calls = {"n": 0}

    async def disconnected():
        calls["n"] += 1
        return calls["n"] >= 5

    # Act
    stream = events_stream(mem_conn, "stream-ka", is_disconnected=disconnected)
    got = [item async for item in stream]

    # Assert：库中无事件 → 仅 keepalive 哨兵
    assert got == [KEEPALIVE_MARK]


@pytest.mark.asyncio
async def test_events_stream_generator_disconnect_stops_loop(mem_conn, monkeypatch):
    """is_disconnected 返回 True → 循环立即退出（不 yield 任何项）。"""
    # Arrange
    monkeypatch.setattr("sgme.operations.events.SSE_POLL_INTERVAL_SEC", 0.01)

    async def disconnected():
        return True  # 首轮检查即断开

    # Act
    stream = events_stream(mem_conn, "stream-off", is_disconnected=disconnected)
    got = [item async for item in stream]

    # Assert
    assert got == []


def test_events_stream_applies_last_event_id_immediately(mem_conn):
    """Last-Event-ID 重连补偿：调用 events_stream 时立即覆盖订阅者游标。"""
    # Arrange：订阅者已有旧游标
    _publish(mem_conn, {"n": 1})
    signal_dao.upsert_subscriber(mem_conn, "sub-reconnect", "old-id", "old-ts")

    # Act：工厂调用即生效（无需迭代流）
    events_stream(mem_conn, "sub-reconnect", last_event_id="new-id")

    # Assert：last_signal_id 被覆盖，last_consumed_ts 原样保留
    sub = signal_dao.get_subscriber(mem_conn, "sub-reconnect")
    assert sub["last_signal_id"] == "new-id"
    assert sub["last_consumed_ts"] == "old-ts"

    # 未提供 last_event_id → 不动游标
    events_stream(mem_conn, "sub-reconnect")
    sub2 = signal_dao.get_subscriber(mem_conn, "sub-reconnect")
    assert sub2["last_signal_id"] == "new-id"


# ---------- 4. http_payload 投影 ----------

def test_http_payload_is_passthrough(mem_conn):
    """http_payload 恒等投影：HTTP 历史形态 == 协议无关超集。"""
    # Arrange
    _publish(mem_conn, {"k": "v"})

    # Act
    data = events_pull(mem_conn, "sub-http", None, 10).data

    # Assert
    assert http_payload(data) is data


# ---------- 5. 契约等价性（最关键） ----------

def test_pull_endpoint_contract_unchanged(client, app):
    """/v1/events/pull 响应与 v0.4 冻结断言逐条一致。"""
    # Arrange
    _publish(app.state.mem_conn, {"hello": "world"})

    # Act
    resp = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "endpoint-sub", "limit": 10},
        headers=AGENT_HEADERS,
    )

    # Assert：test_signal.py::test_events_pull_endpoint 冻结断言
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "events" in body
    assert "next_cursor" in body
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "memory_updated"
    assert body["events"][0]["payload"]["hello"] == "world"
    assert body["next_cursor"] is not None
    # 字段集合不增不减（test_server_v04.py::test_events_pull_returns_events 同款断言）
    assert list(body.keys()) == list(PULL_RESPONSE_KEYS)
    assert list(body["events"][0].keys()) == list(EVENT_ENVELOPE_KEYS)

    # 游标推进语义：第二次拉取为空（test_signal 冻结）
    r2 = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "endpoint-sub", "limit": 10},
        headers=AGENT_HEADERS,
    )
    assert r2.json()["events"] == []
    assert r2.json()["next_cursor"] is None


def test_after_endpoint_contract_unchanged(client, app):
    """GET /v1/events?after= 与 test_server_v04::test_contract_events_after_param 冻结断言一致。"""
    # Arrange
    _publish(app.state.mem_conn, {"k": "v"})

    # Act（冻结的调用形态：after="" + limit=10）
    resp = client.get("/v1/events", params={"after": "", "limit": 10}, headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "events" in body
    assert len(body["events"]) >= 1
    assert "next_cursor" in body


@pytest.mark.asyncio
async def test_stream_endpoint_contract_unchanged(app):
    """SSE 帧格式与 v0.4 冻结断言逐条一致（帧内容逐字节校验）。"""
    # Arrange：发布事件，确保首块快速到达
    _publish(app.state.mem_conn, {"hello": "stream"})

    # Act：直接调用端点函数（test_signal.py 同款 MockRequest 模式）
    from sgme.server.routes_events import event_stream

    class MockRequest:
        def __init__(self, app_state):
            self.app = type("App", (), {"state": app_state})()
            self.headers = {}
            self._disconnected = False

        async def is_disconnected(self):
            return self._disconnected

    request = MockRequest(app.state)
    response = await event_stream(request, subscriber_id="sse-contract", _="key")

    # Assert：media_type 冻结
    assert response.media_type == SSE_MEDIA_TYPE

    received = ""
    gen = response.body_iterator
    try:
        with move_on_after(5):
            async for chunk in gen:
                received += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                if received.endswith("\n\n"):
                    break
    finally:
        await gen.aclose()

    # test_signal.py::test_sse_stream_pushes_events 冻结断言
    assert "id:" in received
    assert "event: memory_updated" in received
    assert "data:" in received
    # test_signal.py::test_events_stream_endpoint 冻结断言（首块以 id: 或 : 开头）
    assert received.startswith("id:") or received.startswith(":")
    for marker in SSE_FRAME_MARKERS:
        assert marker in received

    # 帧格式逐字节冻结：id / event / data 三行 + 帧间空行，data 为事件信封 JSON
    lines = received.split("\n")
    assert lines[0].startswith("id: ")
    assert lines[1] == "event: memory_updated"
    assert lines[2].startswith("data: ")
    assert lines[3] == ""  # 帧间空行
    data = json.loads(lines[2][len("data: "):])
    assert list(data.keys()) == list(EVENT_ENVELOPE_KEYS)
    assert data["event_id"] == lines[0][len("id: "):]
    assert data["payload"] == {"hello": "stream"}


@pytest.mark.asyncio
async def test_stream_last_event_id_reconnect_contract(app):
    """Last-Event-ID 头在进入流前覆盖订阅者游标（原路由同步行为保留）。"""
    # Arrange：订阅者已有旧游标
    mem_conn = app.state.mem_conn
    e1 = _publish(mem_conn, {"n": 1})
    signal_dao.upsert_subscriber(mem_conn, "sse-reconnect", "old-id", "old-ts")

    # Act：带 Last-Event-ID 头调端点（await 即执行路由体，无需迭代流）
    from sgme.server.routes_events import event_stream

    class MockRequest:
        def __init__(self, app_state):
            self.app = type("App", (), {"state": app_state})()
            self.headers = {"Last-Event-ID": e1}
            self._disconnected = False

        async def is_disconnected(self):
            return self._disconnected

    request = MockRequest(app.state)
    response = await event_stream(request, subscriber_id="sse-reconnect", _="key")
    assert response.media_type == SSE_MEDIA_TYPE

    # Assert：游标被覆盖、时间戳保留
    sub = signal_dao.get_subscriber(mem_conn, "sse-reconnect")
    assert sub["last_signal_id"] == e1
    assert sub["last_consumed_ts"] == "old-ts"


# ---------- 6. 鉴权契约不变 ----------

def test_no_key_pull_403_contract(client):
    """无 Key 调 /v1/events/pull → 403 ERR_FORBIDDEN（test_signal 冻结）。"""
    # Act
    resp = client.get("/v1/events/pull", params={"subscriber_id": "no-key-sub"})

    # Assert
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_no_key_stream_403_contract(client):
    """无 Key 调 /v1/events/stream → 403 JSON（test_signal 冻结）。"""
    # Act
    with client.stream("GET", "/v1/events/stream", timeout=5) as resp:
        # Assert
        assert resp.status_code == 403
        assert resp.headers["content-type"].startswith("application/json")


# ---------- 7. operations 直调与 HTTP 端点逐字段一致 ----------

def test_pull_operations_data_equals_http_response(client, app, mem_conn):
    """同一库状态下 operations 直调结果与 HTTP 端点响应逐字段一致。"""
    # Arrange：发布一条事件；两路用不同订阅者避免共享游标互相影响
    _publish(mem_conn, {"n": 1})

    # Act：HTTP 先拉，operations 后拉
    resp = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "http-side", "limit": 10},
        headers=AGENT_HEADERS,
    )
    http_body = resp.json()
    ops_res = events_pull(mem_conn, "ops-side", None, 10)

    # Assert：逐字段一致（事件信封与 next_cursor 全等）
    assert ops_res.ok is True
    assert http_body == ops_res.data
