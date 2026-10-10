"""tests/test_memory_usage_wiring.py：记忆调用统计接线（T-252，2026-10-10）。

覆盖：
1. 端到端：``search()``（memory 层命中）→ memory_usage_daily 出现 search 行；
   agent_id 缺省 → caller 回落 unknown
2. 端到端：``inject()`` mode 分支 / custom_filter 分支 → inject 行（跨 section 去重）
3. 记录失败静默：表缺失 / DAO 抛错时主流程照常（best-effort 旁路）
4. 入口传参：HTTP ``POST /v1/inject``、``POST /v1/search`` 按鉴权 key 反查 caller；
   MCP ``inject`` / ``search`` 工具经 ``_usage_caller(ctx)`` 反查

零真实网络、零 LLM：向量默认关闭（出厂基线 enabled:false），检索走 BM25/LIKE。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.mcp_server import ApiKeyMiddleware, mount_mcp
from sgme.operations import memory_usage as op
from sgme.operations.inject import inject as inject_operation
from sgme.operations.search import search as search_operation
from sgme.profile import tier0 as tier0_mod
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


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
def summary_path(tmp_path, monkeypatch):
    """隔离 tier0 摘要路径（照 test_operations_inject 模式，避免读真实画像文件）。"""
    p = tmp_path / "tier0_summary.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)
    return p


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（复用同一批连接，便于与 operations 直调对照）。"""
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


def _insert(mem_conn, content, dims=None):
    return memory_dao.insert_memory(
        mem_conn,
        content=content,
        memory_type="persona",
        priority=90,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=dims or ["goals"],
    )


def _rows(mem_conn):
    return [
        dict(r)
        for r in mem_conn.execute(
            "SELECT * FROM memory_usage_daily ORDER BY memory_id, source, caller, day"
        ).fetchall()
    ]


# ---------- 1/2. operations 直调：search / inject 双源 ----------


def test_search_memory_layer_records_source_search(conns, cfg):
    mem_conn, session_conn, _ = conns
    mid = _insert(mem_conn, "MemUsageWiringToken 记忆调用统计接线用例")
    res = search_operation(
        mem_conn,
        session_conn,
        cfg,
        query="MemUsageWiringToken",
        scopes=["memory"],
        include_sources=False,
        agent_id="tester-agent",
    )
    assert res.ok is True
    assert any(r.get("memory_id") == mid for r in res.data["results"])
    rows = _rows(mem_conn)
    assert len(rows) == 1
    assert (
        rows[0]["memory_id"],
        rows[0]["source"],
        rows[0]["caller"],
        rows[0]["hits"],
    ) == (mid, "search", "tester-agent", 1)


def test_search_records_caller_unknown_when_absent(conns, cfg):
    """agent_id 缺省（未解析出请求方）→ caller 回落 unknown（值域对齐中间件口径）。"""
    mem_conn, session_conn, _ = conns
    mid = _insert(mem_conn, "CallerAbsentToken 调用方缺省用例")
    res = search_operation(
        mem_conn,
        session_conn,
        cfg,
        query="CallerAbsentToken",
        scopes=["memory"],
        include_sources=False,
    )
    assert res.ok is True
    rows = _rows(mem_conn)
    assert [(r["memory_id"], r["source"], r["caller"]) for r in rows] == [
        (mid, "search", "unknown")
    ]


def test_search_no_hit_writes_nothing(conns, cfg):
    mem_conn, session_conn, _ = conns
    res = search_operation(
        mem_conn,
        session_conn,
        cfg,
        query="NoSuchMemoryToken",
        scopes=["memory"],
        include_sources=False,
    )
    assert res.ok is True
    assert _rows(mem_conn) == []


def test_inject_mode_records_source_inject(conns, cfg, summary_path):
    """mode 分支：section_results 即注入集 → inject 行（跨 section 去重后 hits=1）。"""
    mem_conn, _, _ = conns
    mid = _insert(mem_conn, "模式注入记账用例", dims=["identity", "family"])
    res = inject_operation(mem_conn, cfg, mode="daily", caller="tester-agent")
    assert res.ok is True
    rows = _rows(mem_conn)
    assert rows, "daily 注入应命中测试记忆（前置：identity+family 双标签）"
    assert {(r["memory_id"], r["source"], r["caller"], r["hits"]) for r in rows} == {
        (mid, "inject", "tester-agent", 1)
    }


def test_inject_custom_filter_records_source_inject(conns, cfg):
    """custom_filter 分支：注入结果 → inject 行；caller 缺省 → unknown。"""
    mem_conn, _, _ = conns
    mid = _insert(mem_conn, "自定义过滤记账用例", dims=["goals"])
    res = inject_operation(
        mem_conn,
        cfg,
        custom_filter={"dimensions": ["goals"]},
        caller=None,
    )
    assert res.ok is True
    rows = _rows(mem_conn)
    assert [(r["memory_id"], r["source"], r["caller"]) for r in rows] == [
        (mid, "inject", "unknown")
    ]


# ---------- 3. 记录失败静默 ----------


def test_record_failure_silent_search_and_inject(conns, cfg):
    """表缺失（统计链路故障）→ search / inject 主流程照常（best-effort 旁路）。"""
    mem_conn, session_conn, _ = conns
    mid = _insert(mem_conn, "SilentFailureToken 主流程不受影响")
    mem_conn.execute("DROP TABLE memory_usage_daily")
    mem_conn.commit()

    res = search_operation(
        mem_conn,
        session_conn,
        cfg,
        query="SilentFailureToken",
        scopes=["memory"],
        include_sources=False,
        agent_id="tester",
    )
    assert res.ok is True
    assert any(r.get("memory_id") == mid for r in res.data["results"])

    res2 = inject_operation(
        mem_conn,
        cfg,
        custom_filter={"dimensions": ["goals"]},
        caller="tester",
    )
    assert res2.ok is True


def test_record_memory_usage_silent_on_dao_error(conns, monkeypatch):
    """DAO 抛任何异常 → 包装层吞噬（不抛，只记 WARNING）。"""
    mem_conn, _, _ = conns
    monkeypatch.setattr(
        "sgme.data.memory_usage_dao.record_usage",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    op.record_memory_usage(mem_conn, ["m1"], "search", "default")  # 不抛即过


# ---------- 4. 入口传参：HTTP 路由 ----------


def test_http_inject_route_passes_caller(conns, app, summary_path):
    """POST /v1/inject：env 主 key → caller=default（与 search 路由同款反查）。"""
    mem_conn, _, _ = conns
    mid = _insert(mem_conn, "HTTP 注入调用方用例", dims=["goals"])
    client = TestClient(app)
    resp = client.post(
        "/v1/inject",
        json={"custom_filter": {"dimensions": ["goals"]}},
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 200
    rows = _rows(mem_conn)
    assert [(r["memory_id"], r["source"], r["caller"]) for r in rows] == [
        (mid, "inject", "default")
    ]


def test_http_search_route_records_source_search(conns, app):
    """POST /v1/search：既有 agent_id 反查结果即 caller（T-140 链路复用）。"""
    mem_conn, _, _ = conns
    mid = _insert(mem_conn, "HttpSearchToken 检索记账用例")
    client = TestClient(app)
    resp = client.post(
        "/v1/search",
        json={"query": "HttpSearchToken", "scopes": ["memory"]},
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 200
    rows = _rows(mem_conn)
    assert [(r["memory_id"], r["source"], r["caller"]) for r in rows] == [
        (mid, "search", "default")
    ]


# ---------- 4. 入口传参：MCP inject 工具 ----------


def _mcp_call_tool(client, name, arguments, api_key):
    """完整 MCP 会话流：initialize → initialized → tools/call（照 test_mcp_server 先例）。"""
    headers = {
        "X-API-Key": api_key,
        "Host": "127.0.0.1:9913",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
        headers=headers,
    )
    sid = r.headers.get("mcp-session-id")
    assert sid, f"无 session id: {r.status_code} {r.text[:200]}"
    h2 = {**headers, "Mcp-Session-Id": sid}
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        headers=h2,
    )
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=h2,
    )
    assert r.status_code == 200, f"tools/call 失败: {r.status_code} {r.text[:300]}"
    for line in r.text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                continue
    return None


def test_mcp_inject_tool_passes_caller(conns, app, summary_path):
    """MCP inject 工具经 _usage_caller(ctx) 反查注册 key 绑定 agent_id → caller。"""
    mem_conn, _, _ = conns
    mid = _insert(mem_conn, "MCP 注入调用方用例", dims=["identity", "family"])

    mcp = mount_mcp(app, start_server=False)
    starlette_app = mcp.streamable_http_app()
    starlette_app.add_middleware(ApiKeyMiddleware, key_store=app.state.key_store)

    key = app.state.key_store.register_agent("planner", [])
    with TestClient(starlette_app) as client:
        body = _mcp_call_tool(client, "inject", {"mode": "daily"}, api_key=key)
    assert body is not None and "result" in body, body

    rows = _rows(mem_conn)
    assert {(r["memory_id"], r["source"], r["caller"]) for r in rows} == {
        (mid, "inject", "planner")
    }


def test_mcp_search_tool_passes_caller(conns, app):
    """MCP search 工具经 _usage_caller(ctx) 反查注册 key 绑定 agent_id → caller（T-252 补）。"""
    mem_conn, _, _ = conns
    mid = _insert(mem_conn, "McpSearchToken 检索调用方用例")

    mcp = mount_mcp(app, start_server=False)
    starlette_app = mcp.streamable_http_app()
    starlette_app.add_middleware(ApiKeyMiddleware, key_store=app.state.key_store)

    key = app.state.key_store.register_agent("searcher", [])
    with TestClient(starlette_app) as client:
        body = _mcp_call_tool(client, "search", {"query": "McpSearchToken"}, api_key=key)
    assert body is not None and "result" in body, body

    rows = _rows(mem_conn)
    assert [(r["memory_id"], r["source"], r["caller"]) for r in rows] == [
        (mid, "search", "searcher")
    ]
