"""tests/test_api_usage.py：接口调用统计（T-163）。

覆盖：
1. DAO：record upsert 累加 / 调用方分行 / 按天分桶 / query / prune
2. HTTP 中间件：直调（route 模板归一化 + caller 反查 + 无 key→anonymous）
3. HTTP 中间件：静默性（记录失败不影响请求）
4. HTTP 集成：经真实 app（TestClient）调用端点后落库
5. MCP 中间件：tools/call 解析（工具名 + caller）
6. MCP 中间件：body 完整重放（应用读到原文）；非 tools/call 不记录；坏 JSON 静默
7. GET /v1/admin/usage 端点契约
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, usage_dao
from sgme.mcp_server import ApiKeyMiddleware
from sgme.server.app import (
    AgentKeyStore,
    UsageMiddleware,
    create_app,
)

# ---------- fixtures ----------


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
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
def store(tmp_path):
    """显式 key 的鉴权设施（免受 shell 环境 SGME_AGENT_KEY 干扰）。"""
    return AgentKeyStore(
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        store_path=tmp_path / "agent_keys.json",
    )


# ---------- 1. DAO ----------


def test_record_usage_upsert_accumulates(mem_conn):
    usage_dao.record_usage(mem_conn, "http", "/v1/append", "default")
    usage_dao.record_usage(mem_conn, "http", "/v1/append", "default")
    rows = mem_conn.execute(
        "SELECT name, caller, calls FROM api_usage_daily"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["calls"] == 2


def test_record_usage_splits_by_caller(mem_conn):
    usage_dao.record_usage(mem_conn, "mcp", "search", "default")
    usage_dao.record_usage(mem_conn, "mcp", "search", "workbuddy")
    rows = mem_conn.execute(
        "SELECT caller, calls FROM api_usage_daily ORDER BY caller"
    ).fetchall()
    assert [(r["caller"], r["calls"]) for r in rows] == [
        ("default", 1),
        ("workbuddy", 1),
    ]


def test_record_usage_day_bucketing(mem_conn):
    usage_dao.record_usage(
        mem_conn, "http", "/v1/append", "default", ts="2026-09-12T23:00:00Z"
    )
    usage_dao.record_usage(
        mem_conn, "http", "/v1/append", "default", ts="2026-09-13T01:00:00Z"
    )
    rows = mem_conn.execute(
        "SELECT day, calls FROM api_usage_daily ORDER BY day"
    ).fetchall()
    assert [(r["day"], r["calls"]) for r in rows] == [
        ("2026-09-12", 1),
        ("2026-09-13", 1),
    ]


def test_query_usage_filters_and_aggregates(mem_conn):
    usage_dao.record_usage(mem_conn, "http", "/v1/append", "default")
    usage_dao.record_usage(
        mem_conn, "http", "/v1/append", "default", ts="2026-09-12T10:00:00Z"
    )
    usage_dao.record_usage(mem_conn, "mcp", "search", "default")
    usage_dao.record_usage(
        mem_conn, "http", "/v1/append", "default", ts="2020-01-01T00:00:00Z"
    )

    out = usage_dao.query_usage(mem_conn, days=30)
    by_name = {(r["kind"], r["name"]): r for r in out}
    assert by_name[("http", "/v1/append")]["calls"] == 2  # 老于窗口的行被过滤
    assert by_name[("mcp", "search")]["calls"] == 1

    out_http = usage_dao.query_usage(mem_conn, days=30, kind="http")
    assert all(r["kind"] == "http" for r in out_http)


def test_prune_usage(mem_conn):
    usage_dao.record_usage(
        mem_conn, "http", "/v1/append", "default", ts="2020-01-01T00:00:00Z"
    )
    usage_dao.record_usage(mem_conn, "http", "/v1/append", "default")
    removed = usage_dao.prune_usage(mem_conn, keep_days=400)
    assert removed == 1
    left = mem_conn.execute("SELECT COUNT(*) AS c FROM api_usage_daily").fetchone()["c"]
    assert left == 1


# ---------- 2/3. HTTP 中间件（直调） ----------


async def _run_middleware(mw, scope, receive=None):
    sent = []

    async def send(message):
        sent.append(message)

    async def default_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await mw(scope, receive or default_receive, send)
    return sent


@pytest.mark.asyncio
async def test_http_usage_middleware_records(mem_conn, store):
    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = UsageMiddleware(fake_app, conn=mem_conn, key_store=store)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/admin/demands/abc-123/status",
        "headers": [(b"x-api-key", store.agent_key.encode())],
        "client": ("10.0.0.9", 12345),
        "route": type("R", (), {"path": "/v1/admin/demands/{demand_id}/status"})(),
    }
    sent = await _run_middleware(mw, scope)
    assert sent[0]["status"] == 200  # 响应不受影响

    row = mem_conn.execute("SELECT * FROM api_usage_daily").fetchone()
    assert row["kind"] == "http"
    assert row["name"] == "/v1/admin/demands/{demand_id}/status"  # route 模板归一化
    assert row["caller"] == "default"
    assert row["last_ip"] == "10.0.0.9"


@pytest.mark.asyncio
async def test_http_usage_anonymous_without_key(mem_conn, store):
    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = UsageMiddleware(fake_app, conn=mem_conn, key_store=store)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/health",
        "headers": [],
        "client": None,
    }
    await _run_middleware(mw, scope)

    row = mem_conn.execute("SELECT * FROM api_usage_daily").fetchone()
    assert row["caller"] == "anonymous"
    assert row["name"] == "/v1/health"  # 无 route 时回退原始 path


@pytest.mark.asyncio
async def test_http_usage_silent_on_error(mem_conn, store, monkeypatch):
    """记录过程抛异常（如连接坏）→ 请求照常完成、不抛出。"""

    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = UsageMiddleware(fake_app, conn=mem_conn, key_store=store)
    monkeypatch.setattr(
        "sgme.data.usage_dao.record_usage",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/health",
        "headers": [],
        "client": None,
    }
    sent = await _run_middleware(mw, scope)
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_http_usage_non_http_scope_passthrough(mem_conn, store):
    """非 http scope（如 lifespan）直接透传、不记录。"""
    called = {"n": 0}

    async def fake_app(scope, receive, send):
        called["n"] += 1

    mw = UsageMiddleware(fake_app, conn=mem_conn, key_store=store)
    await mw({"type": "lifespan"}, None, None)
    assert called["n"] == 1
    assert (
        mem_conn.execute("SELECT COUNT(*) AS c FROM api_usage_daily").fetchone()["c"]
        == 0
    )


# ---------- 4. HTTP 集成（真实 app） ----------


def test_http_usage_via_testclient(conns, cfg, tmp_path):
    mem_conn, session_conn, wiki_conn = conns
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    client = TestClient(app)
    resp = client.post(
        "/v1/inject",
        json={"mode": "daily"},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 200

    rows = mem_conn.execute(
        "SELECT name, caller, calls FROM api_usage_daily WHERE kind='http'"
    ).fetchall()
    got = {(r["name"], r["caller"]): r["calls"] for r in rows}
    assert ("/v1/inject", "default") in got  # env 主 key → default


def test_http_usage_route_template_normalization(conns, cfg, tmp_path):
    """带路径参数的端点：记录 name 为路由模板（含 {demand_id}），而非具体 URL。"""
    mem_conn, session_conn, wiki_conn = conns
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    client = TestClient(app)
    # 不存在的 id → 端点内 404，但路由匹配成功（scope["route"] 可用）
    resp = client.get(
        "/v1/admin/demands/11111111-2222-3333-4444-555555555555",
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 404

    row = mem_conn.execute(
        "SELECT name FROM api_usage_daily WHERE kind='http' AND name LIKE '%demand%'"
    ).fetchone()
    assert row is not None
    assert row["name"] == "/v1/admin/demands/{demand_id}"  # 模板而非具体 UUID

    # 未匹配路径（404）→ FastAPI catch-all 兜底 → 记为 "(unmatched)"（防行数爆炸）
    client.get("/v1/definitely-not-a-route", headers={"X-API-Key": "test-admin-key"})
    row2 = mem_conn.execute(
        "SELECT name, calls FROM api_usage_daily WHERE name = '(unmatched)'"
    ).fetchone()
    assert row2 is not None


# ---------- 5/6. MCP 中间件 ----------


def _mcp_scope(headers):
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
        "client": ("10.0.0.8", 5555),
    }


async def _call_mcp_middleware(mem_conn, store, body: bytes, headers=None, chunks=None):
    """直调 ApiKeyMiddleware：fake app 读一次 body 并回 200。"""
    received = {}

    async def fake_app(scope, receive, send):
        msg = await receive()
        received["body"] = msg.get("body")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = ApiKeyMiddleware(fake_app, key_store=store, conn=mem_conn)
    messages = (
        chunks
        if chunks is not None
        else [{"type": "http.request", "body": body, "more_body": False}]
    )

    async def receive():
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    await mw(
        _mcp_scope(headers or [(b"x-api-key", store.agent_key.encode())]), receive, send
    )
    return received, sent


@pytest.mark.asyncio
async def test_mcp_usage_records_tools_call(mem_conn, store):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "x"}},
        }
    ).encode()
    received, _ = await _call_mcp_middleware(mem_conn, store, body)

    assert received["body"] == body  # 完整重放：应用读到原文
    row = mem_conn.execute("SELECT * FROM api_usage_daily").fetchone()
    assert row["kind"] == "mcp"
    assert row["name"] == "search"
    assert row["caller"] == "default"
    assert row["last_ip"] == "10.0.0.8"


@pytest.mark.asyncio
async def test_mcp_usage_ignores_non_tools_call(mem_conn, store):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    ).encode()
    received, _ = await _call_mcp_middleware(mem_conn, store, body)

    assert received["body"] == body  # 原样重放
    assert (
        mem_conn.execute("SELECT COUNT(*) AS c FROM api_usage_daily").fetchone()["c"]
        == 0
    )


@pytest.mark.asyncio
async def test_mcp_usage_bad_json_silent(mem_conn, store):
    body = b"{not-json tools/call"
    received, sent = await _call_mcp_middleware(mem_conn, store, body)

    assert received["body"] == body  # 坏 body 也完整重放
    assert sent[0]["status"] == 200  # 请求不受影响
    assert (
        mem_conn.execute("SELECT COUNT(*) AS c FROM api_usage_daily").fetchone()["c"]
        == 0
    )


@pytest.mark.asyncio
async def test_mcp_usage_replay_multichunk(mem_conn, store):
    """分块 body（more_body=True）也完整重放并记录。"""
    full = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "append", "arguments": {"content": "x"}},
        }
    ).encode()
    mid = len(full) // 2
    chunks = [
        {"type": "http.request", "body": full[:mid], "more_body": True},
        {"type": "http.request", "body": full[mid:], "more_body": False},
    ]
    received, _ = await _call_mcp_middleware(mem_conn, store, b"", chunks=chunks)

    assert received["body"] == full
    row = mem_conn.execute("SELECT name FROM api_usage_daily").fetchone()
    assert row["name"] == "append"


@pytest.mark.asyncio
async def test_mcp_usage_403_not_recorded(mem_conn, store):
    """鉴权失败（403）路径不读 body、不记录。"""
    body = json.dumps({"method": "tools/call", "params": {"name": "search"}}).encode()
    received, sent = await _call_mcp_middleware(
        mem_conn, store, body, headers=[(b"x-api-key", b"wrong-key")]
    )
    assert sent[0]["status"] == 403
    assert "body" not in received  # fake app 没被调用
    assert (
        mem_conn.execute("SELECT COUNT(*) AS c FROM api_usage_daily").fetchone()["c"]
        == 0
    )


@pytest.mark.asyncio
async def test_mcp_usage_disabled_when_conn_none(store):
    """conn=None（未接统计）时请求照常、无记录。"""
    body = json.dumps({"method": "tools/call", "params": {"name": "search"}}).encode()
    received = {}

    async def fake_app(scope, receive, send):
        msg = await receive()
        received["body"] = msg.get("body")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = ApiKeyMiddleware(fake_app, key_store=store, conn=None)
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0)

    sent = []

    async def send(message):
        sent.append(message)

    await mw(_mcp_scope([(b"x-api-key", store.agent_key.encode())]), receive, send)
    assert received["body"] == body
    assert sent[0]["status"] == 200


# ---------- 7. 查询端点 ----------


def test_usage_endpoint_contract(conns, cfg, tmp_path):
    mem_conn, session_conn, wiki_conn = conns
    usage_dao.record_usage(mem_conn, "mcp", "search", "default")
    usage_dao.record_usage(mem_conn, "http", "/v1/append", "workbuddy")

    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    client = TestClient(app)

    # 鉴权：无 key → 403
    assert client.get("/v1/admin/usage").status_code == 403

    resp = client.get("/v1/admin/usage?days=7", headers={"X-API-Key": "test-admin-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["days"] == 7
    kinds = {(i["kind"], i["name"], i["caller"]) for i in data["items"]}
    assert ("mcp", "search", "default") in kinds
    assert ("http", "/v1/append", "workbuddy") in kinds

    # kind 过滤
    resp2 = client.get(
        "/v1/admin/usage?days=7&kind=mcp", headers={"X-API-Key": "test-admin-key"}
    )
    assert {i["kind"] for i in resp2.json()["items"]} == {"mcp"}
