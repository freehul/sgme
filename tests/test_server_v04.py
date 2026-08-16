"""T16 测试：HTTP 服务集成（v0.4 全新端点 + 升级端点回归）。

覆盖：
- Tier0 注入：摘要有效 present:true / 无摘要降级 present:false
- 搜索 RRF：向量可用 RRF 融合 / 向量不可达降级纯 BM25
- 健康检查：返回完整可观测性字段（llm.available / refinement.stalled / heartbeat_ok / last_refined_at）
- 信号端点：/v1/events/pull 返回事件 + next_cursor / /v1/events/stream SSE 可连接
- Admin 端点：refine/trigger 发布 memory_updated + 触发 L2 / tier0/refresh / backup create/list/restore
- 鉴权：新端点 admin key 强制 / events 端点 agent key 强制

mock LLM 用 httpx.MockTransport 拦截 /chat/completions 与 /embeddings。
所有测试 tmp_path 隔离，不污染项目 data/ 与 raw/。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from anyio import move_on_after
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import health as health_mod
from sgme.engine import l2 as l2_mod
from sgme.engine import refine as refine_mod
from sgme.profile import tier0 as tier0_mod
from sgme.raw import store as raw_store
from sgme.data.search import vector as vector_mod
from sgme.server.app import create_app
from sgme.signal import engine as signal_engine
from sgme.data import db as db_mod
from sgme.data import memory_dao, scene_dao, session_dao


# ---------- 公共 fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录（monkeypatch sgme_config.RAW_DIR + raw_store.config）。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def summary_path(tmp_path, monkeypatch):
    """隔离 tier0_summary.json 路径到 tmp_path。"""
    p = tmp_path / "tier0_summary.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)
    return p


@pytest.fixture
def app(tmp_path, cfg, raw_dir, summary_path, monkeypatch):
    """创建隔离的 FastAPI 应用。

    - data_dir / raw_dir / tier0_summary 路径全部隔离到 tmp_path
    - mock LLM 探测（避免 /v1/health 实际打 127.0.0.1:1014）
    - 备份目录指向 tmp_path（避免污染项目 data/backups）
    - 配置落盘经 SGME_CONFIG_PATH 指向 tmp_path/sgme_test.yaml，
      防止 /v1/admin/config 写回真实 config/sgme.yaml（防污染护栏）
    - 清理 SGME_BEARER_TOKEN 环境变量，防止其他测试污染
    """
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    # mock LLM 探测：避免 health 实际打 127.0.0.1:1014
    monkeypatch.setattr(
        health_mod, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "lm-studio",
            "model": "mock-model", "error": None,
        },
    )
    # 备份配置覆盖到 tmp_path
    cfg["backup"] = {
        "dir": str(tmp_path / "backups"),
        "schedule": "0 2 * * *",
        "raw_cold_days": 90,
        "remote_dir": None,
    }
    # data_dir 与连接的库目录对齐：backup manager 从 cfg["paths"]["data_dir"]
    # 取库路径（routes_backup），不覆盖会直连全局 data/ 撞 Gateway 的
    # memory.db-wal 锁（WinError 32，GLM 报告的 test_backup_restore_endpoint 根因）
    cfg["paths"]["data_dir"] = str(tmp_path / "data")
    # 配置落盘隔离：防止 _persist 经 /v1/admin/config 写回真实 config/sgme.yaml
    # （test_contract_config_post_alias 等会触发落盘）
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        data_dir=tmp_path / "data",
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    try:
        db_mod.close(mem_conn)
    except Exception:
        pass
    try:
        db_mod.close(wiki_conn)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _stop_backup_scheduler_after():
    """每个测试后停止 backup_scheduler 常驻线程（防跨文件连接泄漏）。"""
    yield
    from sgme.engine import backup_scheduler
    backup_scheduler.stop_scheduler(timeout=2.0)


@pytest.fixture
def client(app):
    return TestClient(app)


ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg_text(role: str, content: str, ts: str | None = None) -> str:
    ts = ts or _now_iso()
    return f"# {ts} {role}\n{content}\n"


# ---------- mock LLM 工具 ----------

def _mock_llm_client(response_text: str) -> httpx.Client:
    """构造 mock httpx 客户端，对所有 /chat/completions 请求返回固定文本。"""
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_text}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_llm_client_sequence(bodies: list[str]) -> httpx.Client:
    """按顺序返回多个 chat/completions 响应（L1 + L2 串联测试用）。"""
    state = {"i": 0}

    def handler(req):
        i = state["i"]
        state["i"] = i + 1
        body = bodies[min(i, len(bodies) - 1)]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_llm_unavailable_client() -> httpx.Client:
    """所有请求抛 ConnectError（模拟 LLM 不可达）。"""
    def handler(req):
        raise httpx.ConnectError("connection refused")
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_embed_client(embedding: list[float]) -> httpx.Client:
    """构造 mock httpx 客户端，对 /embeddings 请求返回固定向量。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"embedding": list(embedding)}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_embed_unavailable_client() -> httpx.Client:
    """/embeddings 不可达。"""
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_chat_and_embed_client(chat_text: str, embedding: list[float]) -> httpx.Client:
    """同时处理 /chat/completions 与 /embeddings 的 mock 客户端。"""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "/embeddings" in url:
            return httpx.Response(200, json={"data": [{"embedding": list(embedding)}]})
        # 默认 chat completions
        return httpx.Response(200, json={
            "choices": [{"message": {"content": chat_text}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


# ---------- Tier0 注入测试 ----------

def test_inject_tier0_present_when_summary_valid(client, summary_path, monkeypatch):
    """先调 /v1/admin/tier0/refresh（mock LLM 返回摘要）→ 再调 /v1/inject → present=True。"""
    # Arrange：mock generate_summary 返回固定摘要
    expected_summary = "用户是名资深工程师，追求简洁稳定的工程实践。"

    def fake_generate(mem_conn_arg, cfg_arg, client=None):
        return expected_summary

    monkeypatch.setattr(tier0_mod, "generate_summary", fake_generate)

    # Act：先刷新 Tier0 摘要
    refresh_resp = client.post("/v1/admin/tier0/refresh", headers=ADMIN_HEADERS)
    assert refresh_resp.status_code == 200, refresh_resp.text
    assert refresh_resp.json()["status"] == "ok"

    # 再调 inject
    inject_resp = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT_HEADERS)
    assert inject_resp.status_code == 200, inject_resp.text

    # Assert：tier0.present=True 且内容匹配
    body = inject_resp.json()
    assert body["tier0"]["present"] is True
    assert body["tier0"]["content"] == expected_summary


def test_inject_tier0_fallback_when_no_summary(client, summary_path):
    """无摘要文件 → /v1/inject → tier0.present=False（静态降级）。"""
    # Act：未生成摘要直接 inject
    resp = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier0"]["present"] is False
    assert body["tier0"]["content"] is None


# ---------- 搜索 RRF 测试 ----------

def test_search_returns_rrf_when_vector_available(client, monkeypatch):
    """mock embeddings 端点返回向量 → /v1/search 返回 RRF 融合结果（routes 含 rrf）。"""
    # Arrange：插入一条记忆并 mock embed client
    app = client.app
    mem_conn = app.state.mem_conn
    mid = memory_dao.insert_memory(
        mem_conn, content="Python FastAPI 底座设计", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    embedding = [1.0, 0.0, 0.0, 0.0]
    cli = _mock_embed_client(embedding)
    # 为已落库记忆补 embedding
    assert vector_mod.upsert_memory_vector(
        mem_conn, mid, "Python FastAPI 底座设计", app.state.cfg, cli
    ) is True

    # mock /v1/search 内部 embed 调用（routes_memory 调 do_search → vector_mod.embed）
    # 通过 monkeypatch vector_mod.embed 直接返回向量，避免实际打 mock client
    monkeypatch.setattr(vector_mod, "embed", lambda query, cfg, client=None: embedding)

    # Act
    resp = client.post("/v1/search", json={
        "query": "Python",
        "scopes": ["memory"],
        "dimensions": ["tech_stack"],
    }, headers=AGENT_HEADERS)

    # Assert：RRF 融合后该记忆应同时命中 bm25 + vector
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) >= 1
    target = next((r for r in body["results"] if r["memory_id"] == mid), None)
    assert target is not None, "目标记忆未命中"
    # RRF 融合后 routes 应含 rrf / vector
    assert "rrf" in target["routes"]
    assert "vector" in target["routes"]
    assert "bm25" in target["routes"]
    # meta.routes 也应含 rrf
    assert "rrf" in body["meta"]["routes"]


def test_search_fallback_bm25_when_vector_unavailable(client, monkeypatch):
    """embeddings 不可达 → /v1/search 降级纯 BM25 不报错（routes 不含 vector/rrf）。"""
    # Arrange：插入一条记忆
    app = client.app
    mem_conn = app.state.mem_conn
    mid = memory_dao.insert_memory(
        mem_conn, content="Python FastAPI 底座设计", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )

    # mock vector_mod.embed 返回 None（模拟 embeddings 不可达）
    monkeypatch.setattr(vector_mod, "embed", lambda query, cfg, client=None: None)

    # Act
    resp = client.post("/v1/search", json={
        "query": "Python",
        "scopes": ["memory"],
        "dimensions": ["tech_stack"],
    }, headers=AGENT_HEADERS)

    # Assert：BM25 仍命中，但 routes 不含 vector/rrf
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) >= 1
    target = next((r for r in body["results"] if r["memory_id"] == mid), None)
    assert target is not None
    assert "vector" not in target["routes"]
    assert "rrf" not in target["routes"]
    assert "bm25" in target["routes"]


# ---------- 健康检查测试 ----------

def test_health_returns_full_observability_fields(client):
    """/v1/health 返回含 llm.available、refinement.stalled、refinement.heartbeat_ok、refinement.last_refined_at。"""
    # Act（app fixture 已 mock LLM 探测为 available=True）
    resp = client.get("/v1/health")

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0b2"
    # llm 字段
    assert "available" in body["llm"]
    assert body["llm"]["available"] is True
    # refinement 字段
    assert "stalled" in body["refinement"]
    assert "heartbeat_ok" in body["refinement"]
    assert "last_refined_at" in body["refinement"]
    assert "watermark_age_sec" in body["refinement"]
    assert "queue_depth" in body["refinement"]


# ---------- 信号端点测试 ----------

def test_events_pull_returns_events(client):
    """发布事件后 /v1/events/pull 返回事件列表 + next_cursor。"""
    # Arrange：通过 signal_engine 发布事件
    app = client.app
    mem_conn = app.state.mem_conn
    signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"file_id": "f1", "memories_count": 2},
        mem_conn=mem_conn,
    )

    # Act
    resp = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "pull-sub", "limit": 10},
        headers=AGENT_HEADERS,
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "events" in body
    assert "next_cursor" in body
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "memory_updated"
    assert body["events"][0]["payload"]["file_id"] == "f1"
    assert body["next_cursor"] is not None


@pytest.mark.asyncio
async def test_events_stream_sse_connectable(client):
    """/v1/events/stream 可连接（SSE，验证 content-type=text/event-stream）。

    用直接调用端点函数方式，避免 TestClient 在流式响应上阻塞
    （参考 tests/test_signal.py 同样的模式）。
    """
    # Arrange：先发布一条事件，确保 SSE 首块快速到达
    app = client.app
    mem_conn = app.state.mem_conn
    signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"hello": "stream"}, mem_conn=mem_conn,
    )

    # Act：直接调用端点函数获取 StreamingResponse（绕过 TestClient 流式阻塞）
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

    # Assert：content-type 为 text/event-stream
    assert response.media_type == "text/event-stream"

    # 拉首块就退出（验证 SSE 信封可读）
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

    # 首块含 SSE 标记（id: 或 keepalive :）
    assert first_chunk
    assert first_chunk.startswith("id:") or first_chunk.startswith(":")


# ---------- Admin 端点测试 ----------

def test_contract_backup_legacy_path(client):
    """契约 §5 兼容：POST /v1/admin/backup ≡ /v1/admin/backup/create。"""
    resp = client.post("/v1/admin/backup", json={"level": "full"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "snapshot_id" in body
    assert body["level"] == "full"


def test_contract_events_after_param(client):
    """契约 §4.5 兼容：GET /v1/events?after= 等价 pull。"""
    app = client.app
    mem_conn = app.state.mem_conn
    from sgme.signal import engine as signal_engine
    signal_engine.publish(
        event_type="memory_updated", source="refine",
        payload={"k": "v"}, mem_conn=mem_conn,
    )
    resp = client.get("/v1/events", params={"after": "", "limit": 10}, headers=AGENT_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "events" in body
    assert len(body["events"]) >= 1


def test_contract_config_post_alias(client):
    """契约 §5 兼容：POST /v1/admin/config 更新配置（等价 PUT）。"""
    resp = client.post("/v1/admin/config", json={
        "section": "refine",
        "values": {"refine_on_append": True},
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["refine_on_append"] is True
    # 还原
    client.post("/v1/admin/config", json={
        "section": "refine",
        "values": {"refine_on_append": False},
    }, headers=ADMIN_HEADERS)


def test_agent_register_then_revoke(client):
    """§6 Key 吊销：签发→吊销→调用失效。"""
    # 签发
    resp = client.post("/v1/admin/agents/register", json={
        "agent_id": "revoke-me", "scope": ["memory"],
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    new_key = resp.json()["api_key"]
    # 新 key 可用（/v1/search 需 Agent Key）
    r = client.post("/v1/search", json={"query": "测试", "scopes": ["memory"]},
                    headers={"X-API-Key": new_key})
    assert r.status_code == 200
    # 按 agent_id 吊销
    resp = client.delete("/v1/admin/agents/revoke-me", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked"] == 1
    # 吊销后失效
    r = client.post("/v1/search", json={"query": "测试", "scopes": ["memory"]},
                    headers={"X-API-Key": new_key})
    assert r.status_code == 403


def test_agent_register_then_revoke_by_key(client):
    """§6 Key 吊销：按 agent_id 吊销后 key 失效。"""
    resp = client.post("/v1/admin/agents/register", json={
        "agent_id": "revoke-by-key", "scope": [],
    }, headers=ADMIN_HEADERS)
    new_key = resp.json()["api_key"]
    resp = client.delete("/v1/admin/agents/revoke-by-key", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    r = client.post("/v1/search", json={"query": "测试", "scopes": ["memory"]},
                    headers={"X-API-Key": new_key})
    assert r.status_code == 403


def test_agent_revoke_unknown_agent(client):
    """吊销不存在的 agent → 404。"""
    resp = client.delete("/v1/admin/agents/ghost", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


def test_agent_revoke_requires_admin(client):
    """Agent Key 吊销他人 → 403。"""
    resp = client.delete("/v1/admin/agents/someone", headers=AGENT_HEADERS)
    assert resp.status_code == 403


def test_refine_trigger_async_returns_immediately(client, monkeypatch):
    """/v1/admin/refine/trigger_async 立即返回 queued（后台提炼不阻塞）。"""
    app = client.app
    mem_conn = app.state.mem_conn
    session_conn = app.state.session_conn

    # 构造 raw 文件
    fid = "f-trigger-async"
    msgs = [
        {"timestamp": "2026-08-04T10:00:00Z", "role": "user",
         "content": "我用 Python 写后端"},
    ]
    raw_store.write_new_file(
        file_id=fid, session_key="sess_async", started_at="2026-08-04T10:00:00Z",
        agent_id="test", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=raw_store.relative_path(fid),
        session_key="sess_async", started_at="2026-08-04T10:00:00Z",
        agent_id="test", status="new", size=raw_store.file_size(fid),
    )

    resp = client.post("/v1/admin/refine/trigger_async", json={
        "file_id": fid,
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["triggered"] == "async"
    assert body["status"] == "queued"
    # 不等待后台线程（测试隔离），仅验证立即返回语义


def test_refine_trigger_publishes_memory_updated(client, monkeypatch):
    """/v1/admin/refine/trigger 成功后 signal_events 表出现 memory_updated 事件。"""
    # Arrange：构造 raw 文件 + raw_files 行
    app = client.app
    mem_conn = app.state.mem_conn
    session_conn = app.state.session_conn

    fid = "f-trigger-signal"
    msgs = [
        {"timestamp": "2026-08-04T10:00:00Z", "role": "user",
         "content": "我用 Python 写后端"},
        {"timestamp": "2026-08-04T10:01:00Z", "role": "assistant",
         "content": "好的，记下了"},
    ]
    raw_store.write_new_file(
        file_id=fid, session_key="sess_trigger", started_at="2026-08-04T10:00:00Z",
        agent_id="test", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=raw_store.relative_path(fid),
        session_key="sess_trigger", started_at="2026-08-04T10:00:00Z",
        agent_id="test", status="new", size=raw_store.file_size(fid),
    )

    # mock L1 + L2（L2 用空动作列表避免场景聚合）
    l1_body = json.dumps([
        {"content": "用户用 Python 写后端", "dimensions": ["技术栈"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static",
         "source_message_ids": [1]},
    ])
    l2_body = json.dumps([])
    cli = _mock_llm_client_sequence([l1_body, l2_body])
    # 注入 mock client 到 refine 模块（refine_file 通过 client 参数透传）
    original_refine_file = refine_mod.refine_file

    def patched_refine_file(file_id, mem, session, cfg, source_type="session", client=None):
        return original_refine_file(
            file_id, mem, session, cfg, source_type=source_type, client=cli,
        )

    monkeypatch.setattr(refine_mod, "refine_file", patched_refine_file)

    # patch L2 聚合注入 mock client（L2 在 L1.5 落库后执行，client 默认 None）
    original_aggregate = l2_mod.aggregate

    def patched_aggregate(memories, mem_conn, cfg, client=None, **kwargs):
        return original_aggregate(
            memories, mem_conn, cfg, client=cli, **kwargs,
        )

    monkeypatch.setattr(l2_mod, "aggregate", patched_aggregate)

    # Act
    resp = client.post("/v1/admin/refine/trigger", json={
        "file_id": fid,
    }, headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "refined"
    # 查 signal_events：应有 source='refine' 的 memory_updated 事件
    cur = mem_conn.execute(
        "SELECT * FROM signal_events WHERE source='refine' AND type='memory_updated'"
    )
    rows = [dict(r) for r in cur.fetchall()]
    assert len(rows) >= 1
    payload = json.loads(rows[0]["payload"])
    assert payload["file_id"] == fid


def test_refine_trigger_creates_l2_scenes(client, monkeypatch):
    """refine 后 memory.db scenes 表出现场景行（mock LLM 输出 CREATE 动作）。"""
    # Arrange
    app = client.app
    mem_conn = app.state.mem_conn
    session_conn = app.state.session_conn

    fid = "f-trigger-l2"
    msgs = [
        {"timestamp": "2026-08-04T10:00:00Z", "role": "user",
         "content": "我开始学习 Rust 写新项目"},
    ]
    raw_store.write_new_file(
        file_id=fid, session_key="sess_l2_trigger", started_at="2026-08-04T10:00:00Z",
        agent_id="test", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=raw_store.relative_path(fid),
        session_key="sess_l2_trigger", started_at="2026-08-04T10:00:00Z",
        agent_id="test", status="new", size=raw_store.file_size(fid),
    )

    # mock L1 + L2（L2 输出 CREATE 动作）
    new_sid = str(uuid.uuid4())
    l1_body = json.dumps([
        {"content": "用户开始用 Rust 写新项目", "dimensions": ["技术栈"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static",
         "source_message_ids": [1]},
    ])
    l2_body = json.dumps([
        {"action": "create", "target_scene_id": new_sid,
         "merged_content": "# Rust 项目\n用户开始用 Rust 写新项目",
         "reason": "新主题"},
    ])
    cli = _mock_llm_client_sequence([l1_body, l2_body])
    original_refine_file = refine_mod.refine_file

    def patched_refine_file(file_id, mem, session, cfg, source_type="session", client=None):
        return original_refine_file(
            file_id, mem, session, cfg, source_type=source_type, client=cli,
        )

    monkeypatch.setattr(refine_mod, "refine_file", patched_refine_file)

    # patch L2 聚合注入 mock client（L2 在 L1.5 落库后执行，client 默认 None）
    original_aggregate = l2_mod.aggregate

    def patched_aggregate(memories, mem_conn, cfg, client=None, **kwargs):
        return original_aggregate(
            memories, mem_conn, cfg, client=cli, **kwargs,
        )

    monkeypatch.setattr(l2_mod, "aggregate", patched_aggregate)

    # Act
    resp = client.post("/v1/admin/refine/trigger", json={
        "file_id": fid,
    }, headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "refined"
    # memory.db scenes 表出现场景行（id 由系统生成，非 LLM 提供的 new_sid）
    scenes = scene_dao.list_active_scenes(mem_conn, limit=10)
    assert len(scenes) >= 1
    scene = scenes[0]
    assert scene["status"] == "active"
    assert scene["heat"] == 1
    assert "Rust" in scene["content"]


def test_tier0_refresh_endpoint(client, monkeypatch):
    """/v1/admin/tier0/refresh 手动触发成功。"""
    # Arrange：mock generate_summary 返回固定摘要
    expected = "用户是名资深工程师，追求简洁稳定。"

    def fake_generate(mem_conn_arg, cfg_arg, client=None):
        return expected

    monkeypatch.setattr(tier0_mod, "generate_summary", fake_generate)

    # Act
    resp = client.post("/v1/admin/tier0/refresh", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["summary_length"] == len(expected)
    # 摘要文件已落盘
    loaded = tier0_mod.load_summary()
    assert loaded == expected


def test_backup_create_endpoint(client):
    """/v1/admin/backup/create 返回 snapshot_id。"""
    # Act
    resp = client.post(
        "/v1/admin/backup/create",
        json={"level": "full"},
        headers=ADMIN_HEADERS,
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "snapshot_id" in body
    assert body["level"] == "full"
    assert body["snapshot_id"].startswith("full_")


def test_backup_list_endpoint(client):
    """创建快照后 /v1/admin/backup/list 列出快照。"""
    # Arrange：先创建一份快照
    create_resp = client.post(
        "/v1/admin/backup/create",
        json={"level": "full"},
        headers=ADMIN_HEADERS,
    )
    assert create_resp.status_code == 200
    sid = create_resp.json()["snapshot_id"]

    # Act
    resp = client.get("/v1/admin/backup/list", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "snapshots" in body
    assert body["total"] >= 1
    ids = [s["snapshot_id"] for s in body["snapshots"]]
    assert sid in ids


def test_backup_restore_endpoint(client):
    """/v1/admin/backup/restore 恢复成功。"""
    # Arrange：先创建一份快照
    create_resp = client.post(
        "/v1/admin/backup/create",
        json={"level": "full"},
        headers=ADMIN_HEADERS,
    )
    assert create_resp.status_code == 200
    sid = create_resp.json()["snapshot_id"]

    # Act：从快照恢复
    resp = client.post(
        "/v1/admin/backup/restore",
        json={"snapshot_id": sid},
        headers=ADMIN_HEADERS,
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "restored" in body
    assert body["restored"]["snapshot_id"] == sid
    assert "pre_restore_snapshot" in body
    assert body["pre_restore_snapshot"].startswith("pre_restore_")


# ---------- 鉴权测试 ----------

def test_new_endpoints_require_admin_key(client):
    """Agent Key 调 /v1/admin/backup/* → 403。"""
    # backup/create
    r1 = client.post(
        "/v1/admin/backup/create",
        json={"level": "full"},
        headers=AGENT_HEADERS,
    )
    assert r1.status_code == 403
    assert r1.json()["error"]["code"] == "ERR_FORBIDDEN"

    # backup/list
    r2 = client.get("/v1/admin/backup/list", headers=AGENT_HEADERS)
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "ERR_FORBIDDEN"

    # backup/restore
    r3 = client.post(
        "/v1/admin/backup/restore",
        json={"snapshot_id": "x"},
        headers=AGENT_HEADERS,
    )
    assert r3.status_code == 403
    assert r3.json()["error"]["code"] == "ERR_FORBIDDEN"

    # tier0/refresh 也需 admin key
    r4 = client.post("/v1/admin/tier0/refresh", headers=AGENT_HEADERS)
    assert r4.status_code == 403
    assert r4.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_events_endpoints_require_agent_key(client):
    """无 Key 调 /v1/events/pull → 403。"""
    # events/pull 无 Key
    r1 = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "no-key"},
    )
    assert r1.status_code == 403
    assert r1.json()["error"]["code"] == "ERR_FORBIDDEN"

    # events/stream 无 Key
    with client.stream(
        "GET", "/v1/events/stream", timeout=5,
    ) as r2:
        assert r2.status_code == 403
        assert r2.headers["content-type"].startswith("application/json")
