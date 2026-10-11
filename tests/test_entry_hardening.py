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
import time
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from sgme import config as sgme_config
from sgme.security_log import SecurityLog, mask_key  # T-265（ST-55）I4
from sgme.security_state import SecurityState  # T-265（ST-55）I4
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


# ---------- 10. 安全观测端点 + admin 写操作审计（T-265 / ST-55，方案 §2.5/§2.8-3） ----------
#
# I4 验收：admin 端点鉴权不回归；security events/status 鉴权 + date 校验 +
# limit clamp；5 处 admin 写操作（register/revoke/restore/update_request/
# config_update）落 admin_op 审计（key_ref 脱敏）。security_log / security_state
# 由 T-262 接线创建，本卡测试自行注入 app.state（DI 叶子，方案 §四 S5）。


# 固定时区锚点（数据卫生假值：test- 前缀假串，同 test_security_log 惯例）
_T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
_AUDIT_AGENT = "test-audit-agent"


class TestSecurityAdminSurface:
    """security events / status 观测端点（S4）。"""

    @pytest.fixture
    def sec_app(self, conns, cfg, tmp_path):
        """挂好 security_log / security_state 的应用（模拟 T-262 接线后的形态）。"""
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
        # 审计日志（log_dir=tmp 隔离；固定时钟可复现）
        app.state.security_log = SecurityLog(tmp_path / "seclogs", clock=lambda: _T0)
        # 安全状态（注入一条活封禁，供 status 快照形状断言；端点用真实时钟
        # 做 snapshot，故注入 now 也取真实时钟——封禁 900s 内视为活）
        state = SecurityState()
        state.record_failure(
            REMOTE_HOST,
            now=time.time(),
            threshold=1,
            window_sec=300,
            ban_seconds=900,
            ban_max_seconds=3600,
            ban_reset_sec=86400,
        )
        app.state.security_state = state
        return app

    @pytest.fixture
    def sec_client(self, sec_app):
        return TestClient(sec_app)

    # ---------- 鉴权（无 key / agent key → 403；admin key → 200） ----------

    def test_events_requires_admin_key(self, sec_client):
        """无 key 调 security events → 403（admin 端点鉴权不回归）。"""
        resp = sec_client.get("/v1/admin/security/events?date=2026-10-11")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"

    def test_events_agent_key_forbidden(self, sec_client):
        """agent key 调 security events → 403（admin 专用）。"""
        resp = sec_client.get(
            "/v1/admin/security/events?date=2026-10-11",
            headers={"X-API-Key": "cust-agent-key"},
        )
        assert resp.status_code == 403

    def test_status_requires_admin_key(self, sec_client):
        """无 key 调 security status → 403。"""
        resp = sec_client.get("/v1/admin/security/status")
        assert resp.status_code == 403

    def test_events_admin_ok(self, sec_client):
        """admin key + 合法 date → 200 信封（events/date/limit/count）。"""
        resp = sec_client.get(
            "/v1/admin/security/events?date=2026-10-11&limit=10",
            headers={"X-API-Key": "cust-admin-key"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["date"] == "2026-10-11"
        assert body["limit"] == 10
        assert body["count"] == len(body["events"])

    def test_status_admin_ok_snapshot_shape(self, sec_client):
        """admin key 调 status → 200 + snapshot 形状（活封禁含注入键）。"""
        resp = sec_client.get(
            "/v1/admin/security/status", headers={"X-API-Key": "cust-admin-key"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # snapshot() 返回形状（security_state.py::snapshot）
        for k in ("active_bans", "fail_counts", "fail_keys", "ban_entries", "size"):
            assert k in body
        # 注入的封禁（REMOTE_HOST, level=1, until=1000+900）出现在活封禁列表
        keys = [b["key"] for b in body["active_bans"]]
        assert keys == [REMOTE_HOST]
        ban = body["active_bans"][0]
        assert ban["level"] == 1
        assert ban["remaining_sec"] >= 1
        assert body["available"] is True

    # ---------- date 严格校验（防路径穿越） / limit clamp ----------

    def test_events_bad_date_400(self, sec_client):
        """date 非法（穿越形态/格式错）→ 400 ERR_INVALID_ARGS，不进文件路径。"""
        for bad in ("../../etc", "2026/10/11", "2026-1-1", "not-a-date"):
            resp = sec_client.get(
                f"/v1/admin/security/events?date={bad}",
                headers={"X-API-Key": "cust-admin-key"},
            )
            assert resp.status_code == 400, f"date={bad}"
            assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"

    def test_events_limit_clamp(self, sec_client):
        """limit >1000 → 按 1000（clamp）；非整数 → 回退 100；均不 500。"""
        headers = {"X-API-Key": "cust-admin-key"}
        resp = sec_client.get(
            "/v1/admin/security/events?date=2026-10-11&limit=99999", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["limit"] == 1000
        resp = sec_client.get(
            "/v1/admin/security/events?date=2026-10-11&limit=abc", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["limit"] == 100

    def test_events_missing_log_file_empty_list(self, sec_client):
        """日志文件不存在（远期日期）→ 200 空列表（不 404/500）。"""
        resp = sec_client.get(
            "/v1/admin/security/events?date=2020-01-01",
            headers={"X-API-Key": "cust-admin-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["events"] == []
        assert resp.json()["count"] == 0


class TestAdminOpAudit:
    """admin 写操作审计（§2.8-3 五处点名：4 明确 + config 采纳）。"""

    @pytest.fixture
    def conns(self, tmp_path, cfg):
        """类内覆盖模块级 conns：restore 会关闭旧 conn（重开新连接换入
        app.state），teardown 须容错（tests/test_backup.py 同款先例）。"""
        mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
        memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
        yield mem_conn, session_conn, wiki_conn
        for conn in (mem_conn, session_conn, wiki_conn):
            try:
                db_mod.close(conn)
            except Exception:
                pass

    @pytest.fixture
    def audit_app(self, conns, cfg, tmp_path):
        """注入 SecurityLog（tmp_path 隔离）的应用；返回 (app, log, log_dir)。"""
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
        log = SecurityLog(tmp_path / "seclogs", clock=lambda: _T0)
        app.state.security_log = log
        return app, log, tmp_path / "seclogs"

    @staticmethod
    def _admin_ops(audit) -> list[dict]:
        """从注入的 SecurityLog 读回 admin_op 事件（read_events 走读接口）。"""
        return [
            e
            for e in audit.read_events("2026-10-11", limit=1000)
            if e.get("event") == "admin_op"
        ]

    def test_register_agent_audited(self, audit_app):
        """①注册 POST /v1/admin/agents/register → admin_op(success)。"""
        app, audit, _ = audit_app
        resp = TestClient(app).post(
            "/v1/admin/agents/register",
            json={"agent_id": _AUDIT_AGENT},
            headers={"X-API-Key": "cust-admin-key"},
        )
        assert resp.status_code == 200, resp.text
        ops = self._admin_ops(audit)
        assert len(ops) == 1
        rec = ops[0]
        assert rec["op"] == "register_agent"
        assert f"target={_AUDIT_AGENT}" in rec["detail"]
        assert "result=success" in rec["detail"]
        assert f"key_ref={mask_key('cust-admin-key')}" in rec["detail"]
        # 绝不落明文 key（§2.5：key_ref 一律脱敏）
        assert "cust-admin-key" not in json.dumps(rec, ensure_ascii=True)

    def test_revoke_agent_audited(self, audit_app):
        """②吊销 DELETE /v1/admin/agents/{id} → admin_op(success)。"""
        app, audit, _ = audit_app
        client = TestClient(app)
        # 先注册一个可吊销 agent（数据卫生假值；顺带产生一条注册审计）
        client.post(
            "/v1/admin/agents/register",
            json={"agent_id": _AUDIT_AGENT},
            headers={"X-API-Key": "cust-admin-key"},
        )
        resp = client.delete(
            f"/v1/admin/agents/{_AUDIT_AGENT}",
            headers={"X-API-Key": "cust-admin-key"},
        )
        assert resp.status_code == 200, resp.text
        ops = [o for o in self._admin_ops(audit) if o["op"] == "revoke_agent"]
        assert len(ops) == 1
        rec = ops[0]
        assert f"target={_AUDIT_AGENT}" in rec["detail"]
        assert "result=success" in rec["detail"]

    def test_backup_restore_audited(self, audit_app, tmp_path, monkeypatch):
        """③backup restore POST /v1/admin/backup/restore → admin_op(success)。

        完整链路：先 create 真实快照再 restore（restore 替换库文件，
        conn 由 restore 重开——teardown 用注入后的 app.state 引用关闭）。
        """
        app, audit, _ = audit_app
        # 备份目录 + raw/ 隔离到 tmp（防真实 data/backups、raw/ 被触碰）
        app.state.cfg["backup"] = {"dir": str(tmp_path / "backups")}
        raw_root = tmp_path / "raw"
        raw_root.mkdir(exist_ok=True)
        monkeypatch.setattr(sgme_config, "RAW_DIR", raw_root)
        client = TestClient(app)
        headers = {"X-API-Key": "cust-admin-key"}
        r = client.post(
            "/v1/admin/backup/create", json={"level": "full"}, headers=headers
        )
        assert r.status_code == 200, r.text
        snapshot_id = r.json()["snapshot_id"]
        resp = client.post(
            "/v1/admin/backup/restore",
            json={"snapshot_id": snapshot_id},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        ops = [o for o in self._admin_ops(audit) if o["op"] == "backup_restore"]
        assert len(ops) == 1
        rec = ops[0]
        assert f"target={snapshot_id}" in rec["detail"]
        assert "result=success" in rec["detail"]
        # restore 重开的新连接显式关闭（防句柄泄漏；旧 conn 已被 restore 关闭）
        for c in (app.state.mem_conn, app.state.session_conn, app.state.wiki_conn):
            try:
                db_mod.close(c)
            except Exception:
                pass

    def test_update_request_audited(self, audit_app):
        """④update request POST /v1/admin/update/request → admin_op(success)。"""
        app, audit, _ = audit_app
        resp = TestClient(app).post(
            "/v1/admin/update/request",
            json={"target_version": "9.9.9-test"},
            headers={"X-API-Key": "cust-admin-key"},
        )
        assert resp.status_code == 200, resp.text
        ops = [o for o in self._admin_ops(audit) if o["op"] == "update_request"]
        assert len(ops) == 1
        rec = ops[0]
        assert "target=9.9.9-test" in rec["detail"]
        assert "result=success" in rec["detail"]

    def test_config_update_audited(self, audit_app):
        """⑤config 更新 PUT /v1/admin/config → admin_op(success)。

        配置落盘路径由 conftest 全局兜底（SGME_CONFIG_PATH → tmp），不碰真实配置。
        """
        app, audit, _ = audit_app
        resp = TestClient(app).put(
            "/v1/admin/config",
            json={"section": "refine", "values": {"refine_on_append": False}},
            headers={"X-API-Key": "cust-admin-key"},
        )
        assert resp.status_code == 200, resp.text
        ops = [o for o in self._admin_ops(audit) if o["op"] == "config_update"]
        assert len(ops) == 1
        rec = ops[0]
        assert "target=refine" in rec["detail"]
        assert "result=success" in rec["detail"]

    def test_revoke_agent_missing_audited_fail(self, audit_app):
        """fail 侧抽验：吊销不存在的 agent → admin_op(result=fail) 留痕。"""
        app, audit, _ = audit_app
        resp = TestClient(app).delete(
            "/v1/admin/agents/test-never-registered",
            headers={"X-API-Key": "cust-admin-key"},
        )
        assert resp.status_code == 404
        ops = [o for o in self._admin_ops(audit) if o["op"] == "revoke_agent"]
        assert len(ops) == 1
        assert "result=fail" in ops[0]["detail"]

    def test_audit_skipped_without_security_log(self, conns, cfg, tmp_path):
        """fail-open：security_log 未注入（T-262 未接线）→ 端点照常工作不 5xx。"""
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
        resp = TestClient(app).put(
            "/v1/admin/config",
            json={"section": "refine", "values": {"refine_on_append": False}},
            headers={"X-API-Key": "cust-admin-key"},
        )
        assert resp.status_code == 200, resp.text
