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
    assert _is_localhost_source(_req("192.168.1.10")) is False
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
