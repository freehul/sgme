# -*- coding: utf-8 -*-
"""tests/test_privacy_wiring.py：D-65 角色隐私层接线测试。

背景：sgme/operations/privacy_guard.py 实现完却零引用，等于没生效。本测试把
「装配入口按调用方 agent_id 校验角色卡 data.privacy_agents 白名单」钉成契约：

- 角色卡可声明 privacy_agents（缺失/空数组 = 未启用隔离，放行）
- operations 层：白名单内放行、白名单外拒绝 + security_log 审计 + 阈值告警
- HTTP 入口：agent_id 一律从 X-API-Key 反查（不认请求体自称）
- MCP 入口：agent_id 从 Context 的鉴权 key 反查
- 向后兼容：不传 agent_id 的老调用方行为不变
"""

from __future__ import annotations

import contextlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.operations.care import assemble
from sgme.operations.errors import ERR_FORBIDDEN

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def roles_dir(tmp_path, monkeypatch):
    """隔离 roles/ 目录（privacy_agents 随卡文件走）。"""
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    return rd


@pytest.fixture
def persona_dir(tmp_path, monkeypatch):
    pd = tmp_path / "personas"
    pd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "PERSONA_DIR", pd)
    return pd


@pytest.fixture
def conns(tmp_path):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（roles/persona 目录随 tmp_path 隔离）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    pd = tmp_path / "personas"
    pd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "PERSONA_DIR", pd)
    mem_conn, session_conn, wiki_conn = conns
    from sgme.server.app import create_app

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


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 角色卡构造 ----------

def _private_card(agents: list | None = None) -> dict:
    """带隐私白名单的角色卡（默认只允许 hermes 装配）。"""
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "管家",
            "description": "测试用角色卡",
            "system_prompt": "你是{{char}}，{{user}}的管家。",
            "privacy_agents": list(agents if agents is not None else ["hermes"]),
        },
    }


def _open_card() -> dict:
    """未声明 privacy_agents 的角色卡（默认放行全部 agent）。"""
    card = _private_card()
    del card["data"]["privacy_agents"]
    return card


def _assert_denied(res) -> None:
    assert res.ok is False
    assert res.error_code == ERR_FORBIDDEN
    assert "无权" in (res.message or "")
    assert res.data is None


# ---------- 角色卡校验：privacy_agents 必须可声明 ----------

def test_role_card_accepts_privacy_agents(roles_dir):
    """data.privacy_agents 是合法字段，保存后原样读回（否则白名单永远配不上）。"""
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes", "dsh"]))

    card = roles_mod.get_role(roles_dir, "butler")

    assert card is not None
    assert card["data"]["privacy_agents"] == ["hermes", "dsh"]


@pytest.mark.parametrize("bad", ["hermes", [123], ["hermes", "  "], [None]])
def test_role_card_rejects_malformed_privacy_agents(roles_dir, bad):
    """privacy_agents 只接受非空字符串数组（拼错/空白身份不放行）。"""
    card = _private_card()
    card["data"]["privacy_agents"] = bad

    with pytest.raises(ValueError, match="privacy_agents"):
        roles_mod.save_role(roles_dir, "butler", card)


# ---------- operations 层：装配校验 ----------

def test_assemble_allows_whitelisted_agent(conns, cfg, roles_dir):
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))
    mem_conn, _, _ = conns

    res = assemble("butler", mem_conn, cfg, agent_id="hermes")

    assert res.ok is True
    assert res.data["system_prompt"].startswith("你是{{char}}")


def test_assemble_denies_agent_not_in_whitelist(conns, cfg, roles_dir):
    """白名单外 agent 拒绝装配：不返回任何角色卡正文。"""
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))
    mem_conn, _, _ = conns

    res = assemble("butler", mem_conn, cfg, agent_id="codex")

    _assert_denied(res)
    assert res.details["role_id"] == "butler"
    assert res.details["agent_id"] == "codex"
    assert res.details["reason"] == "not_in_whitelist"


def test_assemble_without_policy_allows_any_agent(conns, cfg, roles_dir):
    """未声明 privacy_agents → 未启用隔离，任意 agent 放行（老卡零影响）。"""
    roles_mod.save_role(roles_dir, "butler", _open_card())
    mem_conn, _, _ = conns

    res = assemble("butler", mem_conn, cfg, agent_id="codex")

    assert res.ok is True


def test_assemble_empty_privacy_agents_means_no_policy(conns, cfg, roles_dir):
    """空数组 = 未启用隔离（契约 D-65），不等于「谁都不许」。"""
    roles_mod.save_role(roles_dir, "butler", _private_card([]))
    mem_conn, _, _ = conns

    res = assemble("butler", mem_conn, cfg, agent_id="codex")

    assert res.ok is True


def test_assemble_without_agent_id_keeps_legacy_behavior(conns, cfg, roles_dir):
    """agent_id 缺省（老调用方/进程内直调）→ 行为不变，不做权限判定。"""
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))
    mem_conn, _, _ = conns

    res = assemble("butler", mem_conn, cfg)

    assert res.ok is True
    # 未判定权限的调用不应留下审计噪声（security_log 按需自建，此时应尚未建表）
    has_table = mem_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='security_log'"
    ).fetchone()[0]
    if has_table:
        assert mem_conn.execute("SELECT COUNT(*) FROM security_log").fetchone()[0] == 0


# ---------- 审计与告警 ----------

def test_denial_writes_security_log(conns, cfg, roles_dir):
    """越权尝试写 security_log（event/role/agent 三要素可查）。"""
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))
    mem_conn, _, _ = conns

    assemble("butler", mem_conn, cfg, agent_id="codex")

    rows = mem_conn.execute(
        "SELECT event, role_id, agent_id, detail FROM security_log"
    ).fetchall()
    assert len(rows) == 1
    event, role_id, agent_id, detail = rows[0]
    assert event == "privacy_access_denied"
    assert role_id == "butler"
    assert agent_id == "codex"
    assert "whitelist" in detail


def test_third_denial_same_day_flags_alert(conns, cfg, roles_dir):
    """同 role+agent 当日累计 ≥3 次 → 拒绝结果带 alert 标记（D-65 ③）。"""
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))
    mem_conn, _, _ = conns

    alerts = [
        assemble("butler", mem_conn, cfg, agent_id="codex").details.get("alert")
        for _ in range(3)
    ]

    assert alerts == [False, False, True]


def test_denial_audit_failure_does_not_mask_denial(conns, cfg, roles_dir, monkeypatch):
    """审计表写入炸了也必须回拒绝（审计是旁路，不能把 403 变成 500）。"""
    from sgme.operations import privacy_guard

    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))
    mem_conn, _, _ = conns

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(privacy_guard, "record_denial", _boom)

    res = assemble("butler", mem_conn, cfg, agent_id="codex")

    _assert_denied(res)


# ---------- HTTP 入口：key 反查 ----------

def test_http_assemble_allows_whitelisted_agent(client, app, roles_dir):
    key = app.state.key_store.register_agent("hermes", [])
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))

    r = client.get("/v1/admin/roles/butler/assemble", headers={"X-API-Key": key})

    assert r.status_code == 200, r.text
    assert r.json()["role_name"] == "管家"


def test_http_assemble_denies_other_registered_agent(client, app, roles_dir):
    """注册 key 反查出 codex → 白名单外 → 403，且审计落在 mem.db。"""
    key = app.state.key_store.register_agent("codex", [])
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))

    r = client.get("/v1/admin/roles/butler/assemble", headers={"X-API-Key": key})

    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"]["code"] == "ERR_FORBIDDEN"
    assert "system_prompt" not in body
    row = app.state.mem_conn.execute(
        "SELECT event, role_id, agent_id FROM security_log"
    ).fetchone()
    assert tuple(row) == ("privacy_access_denied", "butler", "codex")


def test_http_assemble_env_key_resolves_default_and_is_denied(client, app, roles_dir):
    """env 主 agent key 反查为 default——同样要过白名单（不能借环境 key 越权）。"""
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))

    r = client.get("/v1/admin/roles/butler/assemble", headers=AGENT_HEADERS)

    assert r.status_code == 403, r.text


def test_http_assemble_env_key_allowed_without_policy(client, roles_dir):
    """老角色卡（无 privacy_agents）+ 老调用方：HTTP 行为不变。"""
    roles_mod.save_role(roles_dir, "butler", _open_card())

    r = client.get("/v1/admin/roles/butler/assemble", headers=AGENT_HEADERS)

    assert r.status_code == 200, r.text


def test_http_role_upsert_then_whitelist_takes_effect(client, app):
    """工作台写卡路径：POST 带 privacy_agents → 落盘生效 → 装配白名单立即校验。

    撤销/新增授权都是改这一份卡文件；assemble 每次读文件，无装配缓存 → 即时生效。
    """
    payload = {
        "data": {
            "name": "私密角色",
            "description": "只允许 hermes 装配",
            "privacy_agents": ["hermes"],
        }
    }
    r = client.post("/v1/admin/roles/secret", json=payload, headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text

    other_key = app.state.key_store.register_agent("codex", [])
    r = client.get(
        "/v1/admin/roles/secret/assemble", headers={"X-API-Key": other_key}
    )
    assert r.status_code == 403, r.text

    hermes_key = app.state.key_store.register_agent("hermes", [])
    r = client.get(
        "/v1/admin/roles/secret/assemble", headers={"X-API-Key": hermes_key}
    )
    assert r.status_code == 200, r.text


# ---------- MCP 入口：ctx 反查 ----------

@contextlib.contextmanager
def _mcp_http_app(tmp_path, monkeypatch):
    """构建带鉴权中间件的 MCP streamable-http app（照 test_mcp_server 样板）。"""
    from sgme.mcp_server import ApiKeyMiddleware, mount_mcp
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "mcp_data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
        agent_store_path=tmp_path / "mcp_agent_keys.json",
    )
    mcp = mount_mcp(app, start_server=False)
    starlette_app = mcp.streamable_http_app()
    starlette_app.add_middleware(ApiKeyMiddleware, key_store=app.state.key_store)
    try:
        # with 进入 lifespan（FastMCP 的 task group 在 lifespan 初始化）
        with TestClient(starlette_app) as client:
            yield client, app
    finally:
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        db_mod.close(wiki_conn)


def _mcp_call_tool(client, name: str, arguments: dict, api_key: str) -> dict:
    """完整 MCP 会话流：initialize → initialized → tools/call（返回解析后的文本 JSON）。"""
    headers = {
        "X-API-Key": api_key,
        "Host": "127.0.0.1:9913",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "pytest-privacy", "version": "1.0"}},
    }, headers=headers)
    sid = r.headers.get("mcp-session-id")
    assert sid, f"无 session id: {r.status_code} {r.text[:200]}"
    h2 = {**headers, "Mcp-Session-Id": sid}
    client.post("/mcp", json={
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }, headers=h2)
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers=h2)
    assert r.status_code == 200, f"tools/call 失败: {r.status_code} {r.text[:300]}"
    for line in r.text.splitlines():
        if line.startswith("data:"):
            body = json.loads(line[5:].strip())
            break
    else:  # pragma: no cover - 协议异常时给出可诊断信息
        raise AssertionError(f"无 SSE data 行: {r.text[:300]}")
    texts = [c.get("text", "") for c in body["result"].get("content", [])]
    return json.loads("\n".join(texts))


def test_mcp_role_assemble_allows_whitelisted_agent(tmp_path, monkeypatch, roles_dir):
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))

    with _mcp_http_app(tmp_path, monkeypatch) as (client, app):
        key = app.state.key_store.register_agent("hermes", [])
        data = _mcp_call_tool(client, "role_assemble", {"role_id": "butler"}, key)

    assert "error" not in data, data
    assert data["role_name"] == "管家"


def test_mcp_role_assemble_denies_other_agent(tmp_path, monkeypatch, roles_dir):
    """MCP 侧同样按鉴权 key 反查——白名单外 agent 拿不到角色卡。"""
    roles_mod.save_role(roles_dir, "butler", _private_card(["hermes"]))

    with _mcp_http_app(tmp_path, monkeypatch) as (client, app):
        key = app.state.key_store.register_agent("codex", [])
        data = _mcp_call_tool(client, "role_assemble", {"role_id": "butler"}, key)
        assert "error" in data, data
        assert "无权" in data["error"]
        row = app.state.mem_conn.execute(
            "SELECT event, role_id, agent_id FROM security_log"
        ).fetchone()

    assert tuple(row) == ("privacy_access_denied", "butler", "codex")
