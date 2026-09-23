"""tests/test_entry_hardening.py：入口层体验加固（Backlog ST-22②④⑧）。

覆盖：
1. 默认开发 Key + 非本机来源 → 403 ERR_FORBIDDEN + 换 Key 引导（ST-22⑧）
2. 默认开发 Key + 本机来源（TestClient 固定 host=testclient）→ 正常放行
3. 自定义 Key + 非本机来源 → 正常放行（不受限）
4. request.client 缺失 → 视为非本机来源，默认 Key 拒绝（安全侧失败）
5. 422 请求体校验失败 → 统一 {"error":{code,message,details}} 结构（ST-22④）
6. 鉴权失败消息含可行动引导（环境变量名 / 注册路径）
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from sgme import config as sgme_config
from sgme.server.app import (
    DEFAULT_AGENT_KEY,
    DEFAULT_ADMIN_KEY,
    AgentKeyStore,
    _is_localhost_source,
    create_app,
    require_agent_key,
)
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.mcp_server import ApiKeyMiddleware

REMOTE_HOST = "203.0.113.9"  # TEST-NET-3 保留地址，仅用于测试


# ---------- fixtures ----------


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
def default_key_app(conns, cfg, tmp_path, monkeypatch):
    """未设置环境变量 → 使用默认开发 Key 的应用（ST-22⑧ 目标场景）。"""
    monkeypatch.delenv("SGME_AGENT_KEY", raising=False)
    monkeypatch.delenv("SGME_ADMIN_KEY", raising=False)
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=None,  # 走 env → 默认兜底
        agent_key=None,
        bearer_token="",  # 显式禁用 Bearer，隔离变量
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def custom_key_app(conns, cfg, tmp_path):
    """自定义 Key 应用（对照：不受来源限制）。"""
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="cust-admin-key",
        agent_key="cust-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


# ---------- 1/2/3. 默认 Key 来源限制（ST-22⑧） ----------


def test_default_agent_key_localhost_ok(default_key_app):
    """默认 Agent Key + 本机来源（testclient）→ 200。"""
    # Arrange / Act
    client = TestClient(default_key_app)
    resp = client.post(
        "/v1/inject", json={"mode": "daily"},
        headers={"X-API-Key": DEFAULT_AGENT_KEY},
    )

    # Assert
    assert resp.status_code == 200, resp.text


def test_default_agent_key_remote_forbidden(default_key_app):
    """默认 Agent Key + 非本机来源 → 403 ERR_FORBIDDEN + 换 Key 引导。"""
    # Arrange / Act
    client = TestClient(default_key_app, client=(REMOTE_HOST, 40000))
    resp = client.post(
        "/v1/inject", json={"mode": "daily"},
        headers={"X-API-Key": DEFAULT_AGENT_KEY},
    )

    # Assert
    assert resp.status_code == 403
    body = resp.json()["error"]
    assert body["code"] == "ERR_FORBIDDEN"
    assert "SGME_AGENT_KEY" in body["message"]
    assert "SGME_ADMIN_KEY" in body["message"]


def test_default_admin_key_remote_forbidden(default_key_app):
    """默认 Admin Key + 非本机来源 → 403 ERR_FORBIDDEN。"""
    # Arrange / Act
    client = TestClient(default_key_app, client=(REMOTE_HOST, 40000))
    resp = client.get("/v1/admin/stats", headers={"X-API-Key": DEFAULT_ADMIN_KEY})

    # Assert
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_default_admin_key_localhost_ok(default_key_app):
    """默认 Admin Key + 本机来源 → 200。"""
    # Arrange / Act
    client = TestClient(default_key_app)
    resp = client.get("/v1/admin/stats", headers={"X-API-Key": DEFAULT_ADMIN_KEY})

    # Assert
    assert resp.status_code == 200, resp.text


def test_custom_key_remote_ok(custom_key_app):
    """自定义 Key + 非本机来源 → 200（不受默认 Key 限制）。"""
    # Arrange / Act
    client = TestClient(custom_key_app, client=(REMOTE_HOST, 40000))
    resp = client.post(
        "/v1/inject", json={"mode": "daily"},
        headers={"X-API-Key": "cust-agent-key"},
    )

    # Assert
    assert resp.status_code == 200, resp.text


# ---------- 4. client 信息缺失 / 辅助判定 ----------


def test_missing_client_info_treated_remote(default_key_app):
    """request.client 缺失 → 视为非本机来源，默认 Key 拒绝（安全侧失败）。"""
    # Arrange：构造无 client 信息的请求 scope
    scope = {
        "type": "http", "method": "POST", "path": "/v1/inject",
        "headers": [(b"x-api-key", DEFAULT_AGENT_KEY.encode())],
        "query_string": b"", "app": default_key_app, "server": ("127.0.0.1", 9910),
    }
    req = Request(scope)

    # Act / Assert
    with pytest.raises(HTTPException) as ei:
        require_agent_key(req)
    assert ei.value.status_code == 403
    assert ei.value.detail["error"]["code"] == "ERR_FORBIDDEN"


def test_is_default_dev_key_unit():
    """is_default_dev_key：仅默认兜底值判定为 True，自定义/None 为 False。"""
    store = AgentKeyStore(admin_key="x", agent_key="y")
    assert store.is_default_dev_key(DEFAULT_AGENT_KEY) is True
    assert store.is_default_dev_key(DEFAULT_ADMIN_KEY) is True
    assert store.is_default_dev_key("custom-key") is False
    assert store.is_default_dev_key(None) is False


def test_is_localhost_source_unit(default_key_app):
    """来源判定：回环集合（含 ::1/localhost/testclient）True，远程/缺失 False。"""

    def _req(host: str | None) -> Request:
        scope = {
            "type": "http", "method": "GET", "path": "/", "headers": [],
            "query_string": b"", "app": default_key_app, "server": ("127.0.0.1", 9910),
        }
        if host is not None:
            scope["client"] = (host, 40000)
        return Request(scope)

    assert _is_localhost_source(_req("127.0.0.1")) is True
    assert _is_localhost_source(_req("::1")) is True
    assert _is_localhost_source(_req("localhost")) is True
    assert _is_localhost_source(_req("testclient")) is True
    assert _is_localhost_source(_req("10.0.0.1")) is False
    assert _is_localhost_source(_req(REMOTE_HOST)) is False
    assert _is_localhost_source(_req(None)) is False


# ---------- 5/6. 统一错误结构（ST-22④） ----------


def test_validation_error_unified_422(custom_key_app):
    """请求体校验失败 → 422 统一 error 结构（此前是 FastAPI 默认 detail 数组）。"""
    # Arrange / Act
    client = TestClient(custom_key_app)
    resp = client.post("/v1/append", json={}, headers={"X-API-Key": "cust-agent-key"})

    # Assert
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "ERR_INVALID_ARGS"
    assert "校验失败" in body["error"]["message"]
    errors = body["error"]["details"]["errors"]
    assert isinstance(errors, list) and errors
    assert all({"loc", "msg", "type"} <= set(e) for e in errors)


def test_auth_error_message_has_guidance(custom_key_app):
    """鉴权失败消息含可行动引导（ST-22④：不是干巴巴的「无效」）。"""
    # Arrange / Act：无 Key 调 Agent 端点
    client = TestClient(custom_key_app)
    resp = client.post("/v1/inject", json={"mode": "daily"})

    # Assert
    assert resp.status_code == 403
    msg = resp.json()["error"]["message"]
    assert "SGME_AGENT_KEY" in msg


def test_admin_auth_error_message_has_guidance(custom_key_app):
    """Admin 鉴权失败消息引导 SGME_ADMIN_KEY。"""
    # Arrange / Act：Agent Key 调 Admin 端点
    client = TestClient(custom_key_app)
    resp = client.get("/v1/admin/stats", headers={"X-API-Key": "cust-agent-key"})

    # Assert
    assert resp.status_code == 403
    assert "SGME_ADMIN_KEY" in resp.json()["error"]["message"]


# ---------- 7. WebUI 密钥自动填充端点 /v1/admin/keys（2026-08-13 用户需求） ----------


def test_keys_endpoint_localhost_returns_keys(default_key_app):
    """本机来源（testclient）→ 返回 admin/agent key，且无需鉴权头。"""
    client = TestClient(default_key_app)
    resp = client.get("/v1/admin/keys")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["admin_key"] == DEFAULT_ADMIN_KEY
    assert data["agent_key"] == DEFAULT_AGENT_KEY


def test_keys_endpoint_custom_keys(custom_key_app):
    """自定义 Key 应用 → 返回配置的 key。"""
    client = TestClient(custom_key_app)
    resp = client.get("/v1/admin/keys")
    assert resp.status_code == 200
    data = resp.json()
    assert data["admin_key"] == "cust-admin-key"
    assert data["agent_key"] == "cust-agent-key"


def test_keys_endpoint_remote_source_forbidden(default_key_app, monkeypatch):
    """远程来源 → 403（防 key 泄漏）。"""
    # TestClient 默认 host=testclient 属本机集合；monkeypatch 来源判定为 False 模拟远程
    monkeypatch.setattr("sgme.server.app._is_localhost_source", lambda req: False)
    client = TestClient(default_key_app)
    resp = client.get("/v1/admin/keys")
    assert resp.status_code == 403
    assert "仅限本机" in resp.json()["error"]["message"]


# ---------- 8. CORS 收敛（F-1，2026-09-24 深度审查） ----------


def _has_acao(resp) -> bool:
    return "access-control-allow-origin" in {k.lower() for k in resp.headers}


def test_cors_evil_origin_gets_no_allow_header(default_key_app):
    """非白名单来源跨源请求 → 无 CORS 许可头（浏览器读不到响应体）。"""
    client = TestClient(default_key_app)
    resp = client.get("/v1/admin/keys", headers={"Origin": "https://evil.example"})

    assert not _has_acao(resp)
    # 端点对「本机来源」的行为不变（CORS 只是浏览器侧放行开关）
    assert resp.status_code == 200


def test_cors_localhost_origin_allowed(default_key_app):
    """本机回环来源（任意端口）→ 回显许可头（本机 HTML 工具不受影响）。"""
    client = TestClient(default_key_app)
    resp = client.get("/v1/admin/keys", headers={"Origin": "http://localhost:8080"})

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:8080"


def test_cors_preflight_evil_origin_denied(default_key_app):
    """OPTIONS 预检带非白名单来源 → 不返回许可头。"""
    client = TestClient(default_key_app)
    resp = client.options(
        "/v1/admin/keys",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert not _has_acao(resp)


def test_cors_configured_origin_allowed(conns, cfg, tmp_path):
    """server.cors_origins 显式登记的来源 → 放行（局域网 HTML 工具的正规入口）。"""
    cfg = dict(cfg)
    cfg["server"] = {**cfg.get("server", {}), "cors_origins": ["http://10.0.0.5:8080"]}
    mem_conn, session_conn, wiki_conn = conns
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="cust-admin-key",
        agent_key="cust-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    resp = TestClient(app).get("/v1/admin/keys", headers={"Origin": "http://10.0.0.5:8080"})

    assert resp.headers.get("access-control-allow-origin") == "http://10.0.0.5:8080"


def test_keys_endpoint_host_header_guard(default_key_app):
    """Host 头非回环（DNS rebinding 形态）→ 403，即使来源地址是回环。"""
    client = TestClient(default_key_app, base_url="http://evil.example")
    resp = client.get("/v1/admin/keys")

    assert resp.status_code == 403
    assert "Host 校验失败" in resp.json()["error"]["message"]


# ---------- 9. MCP 默认 Key 来源限制（F-3，2026-09-24 深度审查） ----------


def _mcp_scope(client_host: str, key: str) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"x-api-key", key.encode())],
        "client": (client_host, 40000),
    }


async def _call_middleware(store, scope) -> list:
    """直调 ApiKeyMiddleware：fake app 直接回 200，收集 sent 消息。"""

    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = ApiKeyMiddleware(fake_app, key_store=store, conn=None)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_mcp_default_key_remote_forbidden():
    """MCP 默认开发 Key + 非本机来源 → 403（与 HTTP 侧同策略；此前缺口在此）。"""
    store = AgentKeyStore(admin_key=DEFAULT_ADMIN_KEY, agent_key=DEFAULT_AGENT_KEY)
    sent = await _call_middleware(store, _mcp_scope(REMOTE_HOST, store.agent_key))

    assert sent[0]["status"] == 403
    body = json.loads(sent[1]["body"].decode("utf-8"))
    assert body["error"]["code"] == "ERR_FORBIDDEN"
    assert "默认开发 Key" in body["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_default_key_localhost_ok():
    """MCP 默认开发 Key + 本机回环 → 放行（本机开发工作流不受影响）。"""
    store = AgentKeyStore(admin_key=DEFAULT_ADMIN_KEY, agent_key=DEFAULT_AGENT_KEY)
    sent = await _call_middleware(store, _mcp_scope("127.0.0.1", store.agent_key))

    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_mcp_custom_key_remote_ok():
    """MCP 自定义 Key + 非本机来源 → 放行（不受默认 Key 限制）。"""
    store = AgentKeyStore(admin_key="cust-admin-key", agent_key="cust-agent-key")
    sent = await _call_middleware(store, _mcp_scope(REMOTE_HOST, store.agent_key))

    assert sent[0]["status"] == 200
