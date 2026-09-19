"""tests/test_skill_usage.py：技能消费统计（T-174，2026-09-19）。

覆盖：
1. DAO：upsert 累加 / 调用方分行 / 按天分桶 / note「最近一次」语义 / query 过滤与聚合 / prune
2. operations：dest_kind 分类 / search_note 截断 / 中间件解析（HTTP 路由模板 + MCP 工具参数）
   / caller_from_state 三种入参 / query_skill_usage 参数校验
3. 埋点分工（HTTP 中间件）：digest+get 落库、search/materialize 不落（业务侧记）、
   caller 写入 scope state 供端点复用
4. 埋点分工（MCP 中间件）：skill_get / skill_digest / skill_materialize（含目标目录类型）
5. 埋点分工（业务侧）：HTTP 端点 digest / search（含命中摘要）/ materialize；
   MCP 工具 skill_search（含命中摘要）
6. ``GET /v1/admin/skills/usage`` 契约（admin key）+ 注册顺序（"usage" 不被当成技能名）

零真实网络、零 LLM：技能业务实现按需 monkeypatch。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, skill_usage_dao
from sgme.mcp_server import ApiKeyMiddleware
from sgme.operations import skill_usage as op
from sgme.operations.errors import OperationResult
from sgme.server.app import AgentKeyStore, UsageMiddleware, create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}
ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}

# Windows 盘符 / UNC 目标目录用拼接构造：既保留真实缺陷信号的形态，又不在源码里
# 落任何本机字面量路径（数据卫生铁律——测试样例同样适用）
WIN_DEST = "D:" + "/tmp/materialized"
UNC_DEST = "\\" + "\\host\\share\\out"

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


def _rows(mem_conn, **where):
    sql = "SELECT * FROM skill_usage_daily"
    params: list = []
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
        params = list(where.values())
    sql += " ORDER BY layer, skill, caller, day"
    return [dict(r) for r in mem_conn.execute(sql, params).fetchall()]


# ---------- 1. DAO ----------


def test_dao_upsert_accumulates(mem_conn):
    skill_usage_dao.record_skill_usage(mem_conn, "get", "nas-ssh", "default")
    skill_usage_dao.record_skill_usage(mem_conn, "get", "nas-ssh", "default")
    rows = _rows(mem_conn)
    assert len(rows) == 1
    assert rows[0]["calls"] == 2


def test_dao_splits_by_caller_and_day(mem_conn):
    """行粒度 = (day, layer, skill, caller)：换调用方或换天都另起一行。"""
    skill_usage_dao.record_skill_usage(
        mem_conn, "get", "nas-ssh", "default", ts="2026-09-18T10:00:00Z"
    )
    skill_usage_dao.record_skill_usage(
        mem_conn, "get", "nas-ssh", "default", ts="2026-09-17T10:00:00Z"
    )
    skill_usage_dao.record_skill_usage(
        mem_conn, "get", "nas-ssh", "dsh", ts="2026-09-18T11:00:00Z"
    )
    rows = _rows(mem_conn, layer="get", skill="nas-ssh")
    assert len(rows) == 3
    assert {r["caller"] for r in rows} == {"default", "dsh"}
    assert {r["day"] for r in rows} == {"2026-09-17", "2026-09-18"}


def test_dao_note_is_last_value_not_clobbered_by_none(mem_conn):
    skill_usage_dao.record_skill_usage(mem_conn, "search", "-", "default", note="hits=2")
    skill_usage_dao.record_skill_usage(mem_conn, "search", "-", "default")  # note=None
    assert _rows(mem_conn)[0]["note"] == "hits=2"  # None 不覆盖已有摘要
    skill_usage_dao.record_skill_usage(mem_conn, "search", "-", "default", note="hits=5")
    assert _rows(mem_conn)[0]["note"] == "hits=5"  # 新摘要覆盖


def test_dao_query_aggregates_and_returns_latest_note(mem_conn):
    skill_usage_dao.record_skill_usage(
        mem_conn, "get", "nas-ssh", "default", note="old", ts="2026-09-17T01:00:00Z"
    )
    skill_usage_dao.record_skill_usage(
        mem_conn, "get", "nas-ssh", "default", note="new", ts="2026-09-18T01:00:00Z"
    )
    skill_usage_dao.record_skill_usage(mem_conn, "digest", "nas-ssh", "dsh")

    items = skill_usage_dao.query_skill_usage(mem_conn, days=400)
    by_layer = {i["layer"]: i for i in items}
    assert by_layer["get"]["calls"] == 2
    assert by_layer["get"]["note"] == "new"  # 最近一次（按 last_ts）
    assert by_layer["digest"]["calls"] == 1

    only_dsh = skill_usage_dao.query_skill_usage(mem_conn, days=400, caller="dsh")
    assert [i["layer"] for i in only_dsh] == ["digest"]
    assert len(skill_usage_dao.query_skill_usage(mem_conn, days=400, skill="nas-ssh")) == 2
    assert skill_usage_dao.query_skill_usage(mem_conn, days=400, layer="search") == []


def test_dao_prune_drops_old_days(mem_conn):
    skill_usage_dao.record_skill_usage(
        mem_conn, "get", "nas-ssh", "default", ts="2024-01-01T00:00:00Z"
    )
    skill_usage_dao.record_skill_usage(mem_conn, "get", "nas-ssh", "default")
    assert skill_usage_dao.prune_skill_usage(mem_conn, keep_days=400) == 1
    assert len(_rows(mem_conn)) == 1


# ---------- 2. operations：纯函数 ----------


@pytest.mark.parametrize(
    "dest,expected",
    [
        (WIN_DEST, "windows"),
        (UNC_DEST, "windows"),
        ("/data/work", "absolute"),
        ("~/work", "home"),
        ("work/sub", "relative"),
        ("", "empty"),
        (None, "empty"),
        ("   ", "empty"),
    ],
)
def test_dest_kind_classifies_without_storing_path(dest, expected):
    assert op.dest_kind(dest) == expected


def test_search_note_truncates_and_flattens_whitespace():
    note = op.search_note("内网\n NAS   容器" + "长" * 200, 3, "nas-ssh")
    assert note.startswith("q=内网 NAS 容器")
    assert "hits=3" in note and "top=nas-ssh" in note
    assert len(note) <= len("q=") + op.QUERY_MAX + len(";hits=3;top=nas-ssh")
    assert "\n" not in note
    assert op.search_note("x", 0, None).endswith("top=-")


def test_extract_middleware_skill_call_routes():
    assert op.extract_middleware_skill_call(
        "/v1/skills/{name}/digest", {"name": "nas-ssh"}
    ) == ("digest", "nas-ssh")
    assert op.extract_middleware_skill_call(
        "/v1/skills/{name}", {"name": "nas-ssh"}
    ) == ("get", "nas-ssh")
    # search / materialize 归业务侧记（需要响应结果 / 请求体语义）
    assert op.extract_middleware_skill_call("/v1/skills/search", {}) is None
    assert op.extract_middleware_skill_call("/v1/skills/{name}/materialize", {}) is None
    assert op.extract_middleware_skill_call("/v1/skills", {}) is None
    assert op.extract_middleware_skill_call("/v1/memory/search", {"name": "x"}) is None
    assert op.extract_middleware_skill_call(None, None) is None
    # 缺 name 时退化为 "-"（不丢调用事实）
    assert op.extract_middleware_skill_call("/v1/skills/{name}", {}) == ("get", "-")


def test_extract_mcp_middleware_call_routes_and_dest_note():
    assert op.extract_mcp_middleware_call("skill_get", {"name": "nas-ssh"}) == (
        "get",
        "nas-ssh",
        None,
    )
    assert op.extract_mcp_middleware_call("skill_digest", {"name": "nas-ssh"}) == (
        "digest",
        "nas-ssh",
        None,
    )
    layer, skill, note = op.extract_mcp_middleware_call(
        "skill_materialize", {"name": "nas-ssh", "dest_dir": WIN_DEST}
    )
    assert (layer, skill) == ("materialize", "nas-ssh")
    assert note == "dest=windows"  # 只记类型，不记真实路径
    # skill_search 由工具侧记（要命中结果）
    assert op.extract_mcp_middleware_call("skill_search", {"query": "x"}) is None
    assert op.extract_mcp_middleware_call("search", {"query": "x"}) is None
    assert op.extract_mcp_middleware_call(None, None) is None


def test_caller_from_state_accepts_dict_object_and_none():
    assert op.caller_from_state({"usage_caller": "dsh"}) == "dsh"
    assert op.caller_from_state(SimpleNamespace(usage_caller="doubao-work")) == "doubao-work"
    assert op.caller_from_state({}) == "unknown"
    assert op.caller_from_state(SimpleNamespace()) == "unknown"
    assert op.caller_from_state(None) == "unknown"


def test_query_skill_usage_validation(mem_conn):
    bad = op.query_skill_usage(mem_conn, layer="wiki")
    assert bad.ok is False and bad.error_code == "ERR_INVALID_ARGS"
    bad2 = op.query_skill_usage(mem_conn, days="lots")  # type: ignore[arg-type]
    assert bad2.ok is False and bad2.error_code == "ERR_INVALID_ARGS"

    ok = op.query_skill_usage(mem_conn, days=99999, limit=99999)
    assert ok.ok is True and ok.data is not None
    assert ok.data["days"] == op.MAX_DAYS
    assert ok.data["limit"] == op.MAX_LIMIT
    assert ok.data["items"] == []
    assert set(ok.data["layers"]) == set(skill_usage_dao.LAYERS)


# ---------- 3. HTTP 中间件埋点 ----------


def _http_scope(route_path: str, path_params: dict | None, headers=None):
    return {
        "type": "http",
        "method": "GET",
        "path": route_path,
        "route": SimpleNamespace(path=route_path) if route_path else None,
        "path_params": path_params or {},
        "headers": headers or [(b"x-api-key", b"test-agent-key")],
        "client": ("10.0.0.9", 5555),
        "state": {},
    }


async def _run_http_middleware(mem_conn, store, scope):
    """直调 UsageMiddleware：fake app 立刻发响应头（记录时机=response.start）。"""
    sent = []

    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def send(message):
        sent.append(message)

    mw = UsageMiddleware(fake_app, conn=mem_conn, key_store=store)
    await mw(scope, None, send)
    return sent


@pytest.mark.asyncio
async def test_http_middleware_records_digest_and_get_only(mem_conn, store):
    """中间件记 digest / get；search / materialize 留给业务侧（同调用不重复计数）。"""
    for path, params in (
        ("/v1/skills/{name}/digest", {"name": "nas-ssh"}),
        ("/v1/skills/{name}", {"name": "nas-ssh"}),
        ("/v1/skills/search", {}),
        ("/v1/skills/{name}/materialize", {"name": "nas-ssh"}),
    ):
        scope = _http_scope(path, params)
        sent = await _run_http_middleware(mem_conn, store, scope)
        assert sent and sent[0]["status"] == 200
        # caller 透传：供技能端点补记时复用（免二次反查）
        assert scope["state"]["usage_caller"] == "default"

    rows = _rows(mem_conn)
    assert {(r["layer"], r["skill"]) for r in rows} == {
        ("digest", "nas-ssh"),
        ("get", "nas-ssh"),
    }
    assert all(r["caller"] == "default" for r in rows)
    assert all(r["last_ip"] == "10.0.0.9" for r in rows)


# ---------- 4. MCP 中间件埋点 ----------


def _mcp_scope(headers=None):
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers or [(b"x-api-key", b"test-agent-key")],
        "client": ("10.0.0.8", 6666),
    }


async def _call_mcp_middleware(mem_conn, store, body: bytes, headers=None):
    async def fake_app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = ApiKeyMiddleware(fake_app, key_store=store, conn=mem_conn)
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        pass

    await mw(_mcp_scope(headers), receive, send)


def _tools_call(name: str, arguments: dict) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode()


@pytest.mark.asyncio
async def test_mcp_middleware_records_get_digest_materialize(mem_conn, store):
    await _call_mcp_middleware(mem_conn, store, _tools_call("skill_get", {"name": "nas-ssh"}))
    await _call_mcp_middleware(
        mem_conn, store, _tools_call("skill_digest", {"name": "nas-ssh"})
    )
    await _call_mcp_middleware(
        mem_conn,
        store,
        _tools_call("skill_materialize", {"name": "nas-ssh", "dest_dir": WIN_DEST}),
    )
    # search 由工具侧记 → 中间件不得记
    await _call_mcp_middleware(
        mem_conn, store, _tools_call("skill_search", {"query": "docker"})
    )

    rows = {(r["layer"], r["skill"]): r for r in _rows(mem_conn)}
    assert set(rows) == {
        ("get", "nas-ssh"),
        ("digest", "nas-ssh"),
        ("materialize", "nas-ssh"),
    }
    assert rows[("materialize", "nas-ssh")]["note"] == "dest=windows"
    assert all(r["caller"] == "default" for r in rows.values())


# ---------- 5. 业务侧埋点：MCP 工具 skill_search ----------


def test_mcp_tool_skill_search_records_search_note(conns, store):
    from sgme import mcp_server

    mem_conn = conns[0]
    mcp_server._app_state["mem_conn"] = mem_conn
    mcp_server._app_state["key_store"] = store
    try:
        mcp_server._record_search_usage(
            "内网 NAS 容器", [{"name": "nas-ssh"}, {"name": "nas-kanban-team"}], None
        )
    finally:
        mcp_server._app_state.pop("mem_conn", None)
        mcp_server._app_state.pop("key_store", None)

    row = _rows(mem_conn)[0]
    assert (row["layer"], row["skill"]) == ("search", "-")
    assert row["caller"] == "anonymous"  # 直调无 key → anonymous（与 HTTP 侧同语义）
    assert row["note"] == "q=内网 NAS 容器;hits=2;top=nas-ssh"


def test_mcp_tool_skill_search_silent_without_conn(conns):
    """未接线（mem_conn 缺失）时静默返回——统计是旁路，不得影响检索。"""
    from sgme import mcp_server

    mcp_server._app_state.pop("mem_conn", None)
    mcp_server._record_search_usage("x", [{"name": "y"}], None)


# ---------- 6. 集成：真实 app 的端点埋点 ----------


@pytest.fixture
def skill_app(tmp_path, monkeypatch, cfg):
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", raw)
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))

    src = tmp_path / "skillsrc"
    src.mkdir()
    cfg["skills"] = {"enabled": True, "source_dirs": [str(src)]}
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
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
    yield app, mem_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def test_http_endpoint_digest_records_layer(skill_app, monkeypatch):
    app, mem_conn = skill_app
    monkeypatch.setattr(
        "sgme.operations.skills.skill_digest",
        lambda *a, **k: OperationResult.succeed({"name": "alpha-skill", "sections": []}),
    )
    client = TestClient(app)
    resp = client.get("/v1/skills/alpha-skill/digest", headers=AGENT_HEADERS)
    assert resp.status_code == 200
    rows = _rows(mem_conn)
    assert [(r["layer"], r["skill"], r["caller"]) for r in rows] == [
        ("digest", "alpha-skill", "default")
    ]


def test_http_endpoint_search_records_note(skill_app, monkeypatch):
    app, mem_conn = skill_app
    monkeypatch.setattr(
        "sgme.operations.skills.search_skills",
        lambda *a, **k: [{"name": "alpha-skill", "score": 0.9}],
    )
    client = TestClient(app)
    resp = client.get("/v1/skills/search?q=docker", headers=AGENT_HEADERS)
    assert resp.status_code == 200
    row = _rows(mem_conn)[0]
    assert (row["layer"], row["skill"], row["caller"]) == ("search", "-", "default")
    assert row["note"] == "q=docker;hits=1;top=alpha-skill"


def test_http_endpoint_materialize_records_dest_kind(skill_app, monkeypatch):
    app, mem_conn = skill_app
    monkeypatch.setattr(
        "sgme.operations.skills.materialize",
        lambda *a, **k: OperationResult.succeed({"name": "alpha-skill", "sha256": "0" * 8}),
    )
    client = TestClient(app)
    resp = client.post(
        "/v1/skills/alpha-skill/materialize",
        json={"dest_dir": WIN_DEST},
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 200
    row = _rows(mem_conn)[0]
    assert (row["layer"], row["skill"], row["caller"]) == (
        "materialize",
        "alpha-skill",
        "default",
    )
    assert row["note"] == "dest=windows"


# ---------- 7. GET /v1/admin/skills/usage ----------


def test_admin_skills_usage_contract_and_registration_order(skill_app):
    app, mem_conn = skill_app
    skill_usage_dao.record_skill_usage(mem_conn, "get", "nas-ssh", "dsh")
    skill_usage_dao.record_skill_usage(mem_conn, "search", "-", "workbuddy", note="hits=1")
    client = TestClient(app)

    resp = client.get("/v1/admin/skills/usage", headers=ADMIN_HEADERS)
    assert resp.status_code == 200  # 未被 /v1/admin/skills/{name} 吞成「技能不存在」
    body = resp.json()
    assert body["total_rows"] == 2
    assert {i["layer"] for i in body["items"]} == {"get", "search"}

    filtered = client.get(
        "/v1/admin/skills/usage?layer=get&skill=nas-ssh", headers=ADMIN_HEADERS
    )
    assert filtered.json()["total_rows"] == 1
    assert filtered.json()["items"][0]["caller"] == "dsh"

    assert client.get("/v1/admin/skills/usage", headers=AGENT_HEADERS).status_code == 403
    assert (
        client.get("/v1/admin/skills/usage?layer=wiki", headers=ADMIN_HEADERS).status_code
        == 400
    )
