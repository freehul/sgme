"""A1-T01 测试：GET /v1/admin/agents（只读 Agent 列表端点）。

覆盖 QA-01 ~ QA-06：
- QA-01：无 X-API-Key / Agent Key / 错误 Key → 403；Bearer 开启且缺失 → 401
- QA-02：🔴 响应不含任何明文 API Key（全文正则扫 agt_[0-9a-f]{32} 与 env key 值）
- QA-03：合成条目 agent_id="default" 被过滤
- QA-04：last_seen_at 由 raw_files 聚合正确；无记录为 null
- QA-05：空注册表 → {"agents": [], "count": 0, ...}
- QA-06：响应含 endpoint 字段位且恒为 null

附加：同一 agent_id 多把 Key 聚合（key_count）、role / active_within_sec 过滤、
wiki.db 不可读时仍 200。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.server.app import AgentKeyStore, create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao


ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}
AGENT_HEADERS = {"X-API-Key": AGENT_KEY}

# 明文 Key 形态：register_agent 签发的 agt_<uuid4.hex>
PLAINTEXT_KEY_RE = re.compile(r"agt_[0-9a-f]{32}")


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """隔离的 memory.db / session.db / wiki.db。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def no_bearer(monkeypatch):
    """清除 SGME_BEARER_TOKEN。

    ⚠️ create_app() 会 os.environ.setdefault("SGME_BEARER_TOKEN", bearer)，
    这是进程级全局副作用——任一测试启用 Bearer 后会污染后续测试。
    用 monkeypatch 隔离，保证本文件用例与执行顺序无关。
    """
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)


@pytest.fixture
def app(cfg, conns, no_bearer, tmp_path):
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        # 隔离：agent key 存储走 tmp（PR#11 前缺省写真实 data/agent_keys.json 会污染生产文件）
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_raw(
    session_conn: sqlite3.Connection,
    file_id: str,
    agent_id: str | None,
    started_at: str | None,
    ended_at: str | None = None,
) -> None:
    """直接写一条 raw_files 记录（只为构造 last_seen 聚合场景）。"""
    session_conn.execute(
        "INSERT INTO raw_files (file_id, path, session_key, agent_id, started_at, ended_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'new')",
        (file_id, f"raw/{file_id}.md", f"sess-{file_id}", agent_id, started_at, ended_at),
    )
    session_conn.commit()


# ---------- QA-01 鉴权 ----------

def test_agents_without_api_key_returns_403(client):
    """QA-01：无 X-API-Key → 403 ERR_FORBIDDEN。"""
    resp = client.get("/v1/admin/agents")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_agents_with_agent_key_returns_403(client):
    """QA-01：Agent Key（非管理员）→ 403。"""
    resp = client.get("/v1/admin/agents", headers=AGENT_HEADERS)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_agents_with_wrong_key_returns_403(client):
    """QA-01：错误 Key → 403。"""
    resp = client.get("/v1/admin/agents", headers={"X-API-Key": "totally-wrong"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_agents_missing_bearer_returns_401(cfg, conns, monkeypatch):
    """QA-01：Bearer 开启但缺失 Authorization → 401 ERR_UNAUTHORIZED。"""
    mem_conn, session_conn, wiki_conn = conns
    # create_app 会 setdefault 环境变量（进程级副作用）；先用 monkeypatch 占位，
    # 使其成为 no-op，并在用例结束后自动还原，避免污染其它测试。
    monkeypatch.setenv("SGME_BEARER_TOKEN", "tok-123")
    bearer_app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        bearer_token="tok-123",
    )
    c = TestClient(bearer_app)

    resp = c.get("/v1/admin/agents", headers=ADMIN_HEADERS)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "ERR_UNAUTHORIZED"

    ok = c.get(
        "/v1/admin/agents",
        headers={**ADMIN_HEADERS, "Authorization": "Bearer tok-123"},
    )
    assert ok.status_code == 200, ok.text


# ---------- QA-02 脱敏 ----------

def test_register_agent_duplicate_id_conflict(client):
    """QA-脱重：同一 agent_id 重复注册 → 409 ERR_CONFLICT，不签发新 Key。"""
    r1 = client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": ["projects"]},
        headers=ADMIN_HEADERS,
    )
    assert r1.status_code == 200, r1.text

    r2 = client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": ["read"]},
        headers=ADMIN_HEADERS,
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "ERR_CONFLICT"

    # 未签发新 Key：列表里 hermes 仍只有 1 把
    agents = client.get("/v1/admin/agents", headers=ADMIN_HEADERS).json()["agents"]
    hermes = next(a for a in agents if a["agent_id"] == "hermes")
    assert hermes["key_count"] == 1


def test_register_agent_reserved_default_conflict(client):
    """QA-脱重：agent_id="default"（env 合成条目）不可注册 → 409。"""
    r = client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "default"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "ERR_CONFLICT"


def test_agents_response_contains_no_plaintext_key(client):
    r = client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": ["projects"]},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    issued_key = r.json()["api_key"]
    assert PLAINTEXT_KEY_RE.fullmatch(issued_key), issued_key

    resp = client.get("/v1/admin/agents", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body_text = resp.text

    assert issued_key not in body_text
    assert PLAINTEXT_KEY_RE.search(body_text) is None
    assert AGENT_KEY not in body_text
    assert ADMIN_KEY not in body_text

    agent = resp.json()["agents"][0]
    assert "key" not in agent
    assert "api_key" not in agent
    # 脱敏指纹：前 6 + … + 后 2
    assert agent["key_ref"] == f"{issued_key[:6]}…{issued_key[-2:]}"


def test_mask_key_never_leaks_short_keys():
    """QA-02：过短 Key 无法安全截断 → 整体隐藏。"""
    assert AgentKeyStore._mask_key("") == ""
    assert AgentKeyStore._mask_key("short") == "…"
    assert AgentKeyStore._mask_key("12345678") == "…"
    assert AgentKeyStore._mask_key("agt_0123456789") == "agt_01…89"


# ---------- QA-03 过滤 default ----------

def test_agents_filters_default_synthetic_entry(client):
    """QA-03：合成条目 agent_id="default" 不出现在响应中。"""
    client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": []},
        headers=ADMIN_HEADERS,
    )
    resp = client.get("/v1/admin/agents", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    ids = [a["agent_id"] for a in resp.json()["agents"]]
    assert "default" not in ids
    assert ids == ["hermes"]


def test_list_agents_public_filters_default_but_list_agents_keeps_it():
    """QA-03：脱敏方法过滤 default；旧 list_agents() 行为保持不变（不回归）。"""
    store = AgentKeyStore(admin_key="a-key", agent_key="b-key")
    store.register_agent("hermes", ["projects"])

    public_ids = [a["agent_id"] for a in store.list_agents_public()]
    assert public_ids == ["hermes"]

    legacy_ids = [a["agent_id"] for a in store.list_agents()]
    assert "default" in legacy_ids  # 旧方法未被改动


def test_agent_keys_file_permissions_restricted(tmp_path):
    """安全加固 2026-08-11：落盘后权限收紧——POSIX 0600 / Windows icacls 去继承。

    断言策略：
    - POSIX：直接验证文件 mode == 0o600
    - Windows：验证文件不再继承父目录 ACL（icacls 输出含 "inheritance disabled"）。
      若环境无 icacls 或执行失败，_restrict_file_permissions 仅告警不抛错——测试
      只验证"未抛异常 + 文件可读"，权限收紧本身由告警兜底。
    """
    import os
    import stat
    import sys

    from sgme.server.app import _restrict_file_permissions

    path = tmp_path / "agent_keys.json"
    path.write_text("{}", encoding="utf-8")

    # 直接调用收紧密封（不经过 _save_to_file，纯函数测试）
    _restrict_file_permissions(path)

    if os.name == "nt":
        assert path.exists()  # Windows 上 icacls 失败只告警不删除文件
        # 若 icacls 可用，验证继承被禁用
        import subprocess

        proc = subprocess.run(["icacls", str(path)], capture_output=True, text=True, errors="replace", timeout=15)
        if proc.returncode == 0:
            # 中文系统输出含 GBK 乱码，只断言 ASCII 安全部分：
            # 收紧后 ACL 只含当前用户 (R,W)，不再继承父目录条目
            assert ":(R,W)" in proc.stdout, proc.stdout
    else:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


def test_save_to_file_triggers_permission_restriction(tmp_path, monkeypatch):
    """安全加固 2026-08-11：register_agent 落盘必经 _restrict_file_permissions。"""
    from sgme.server import app as app_mod

    calls = []
    monkeypatch.setattr(app_mod, "_restrict_file_permissions", lambda p: calls.append(str(p)))

    store = AgentKeyStore(admin_key="a-key", agent_key="b-key", store_path=tmp_path / "agent_keys.json")
    store.register_agent("hermes", ["projects"])

    assert len(calls) == 1
    assert calls[0].endswith("agent_keys.json")


# ---------- QA-04 last_seen_at 聚合 ----------

def test_agents_last_seen_at_aggregated_from_raw_files(client, conns):
    """QA-04：last_seen_at = MAX(COALESCE(ended_at, started_at)) GROUP BY agent_id。"""
    _, session_conn, _ = conns
    for aid in ("hermes", "planner"):
        client.post(
            "/v1/admin/agents/register",
            json={"agent_id": aid, "scope": []},
            headers=ADMIN_HEADERS,
        )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = _iso(now - timedelta(hours=5))
    mid = _iso(now - timedelta(hours=2))
    new = _iso(now - timedelta(minutes=10))

    # hermes：两条记录，取更晚的；第二条只有 started_at（COALESCE 回落）
    _insert_raw(session_conn, "f1", "hermes", old, mid)
    _insert_raw(session_conn, "f2", "hermes", new, None)
    # planner：无 raw_files 记录 → null

    resp = client.get("/v1/admin/agents", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    by_id = {a["agent_id"]: a for a in resp.json()["agents"]}

    assert by_id["hermes"]["last_seen_at"] == new
    assert by_id["hermes"]["last_seen_source"] == "append"
    assert by_id["planner"]["last_seen_at"] is None
    assert by_id["planner"]["last_seen_source"] is None


def test_agents_still_200_when_raw_files_unreadable(client, conns):
    """QA-04：raw_files 不可读 → 仍 200，last_seen_at 全 null（不返 500）。"""
    _, session_conn, _ = conns
    client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": []},
        headers=ADMIN_HEADERS,
    )
    session_conn.execute("DROP TABLE raw_files")
    session_conn.commit()

    resp = client.get("/v1/admin/agents", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["agents"][0]["agent_id"] == "hermes"
    assert body["agents"][0]["last_seen_at"] is None


# ---------- QA-05 空注册表 ----------

def test_agents_empty_registry(client):
    """QA-05：空注册表 → agents=[]，count=0，顶层字段齐全。"""
    resp = client.get("/v1/admin/agents", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agents"] == []
    assert body["count"] == 0
    assert body["source"] == "sgme.key_store"
    assert isinstance(body["generated_at"], str) and body["generated_at"]
    assert body["snapshot_at"] == body["generated_at"]


# ---------- QA-06 endpoint 字段位 ----------

def test_agents_endpoint_field_present_and_null(client):
    """QA-06：每条含 endpoint 字段位，且恒为 null。"""
    client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": ["projects"]},
        headers=ADMIN_HEADERS,
    )
    resp = client.get("/v1/admin/agents", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    agent = resp.json()["agents"][0]
    assert "endpoint" in agent
    assert agent["endpoint"] is None


# ---------- 契约字段完整性 / 聚合 / 过滤 ----------

def test_agents_schema_fields(client):
    """响应每条 Agent 的字段集与契约 §5.2 严格一致。"""
    client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": ["projects"]},
        headers=ADMIN_HEADERS,
    )
    body = client.get("/v1/admin/agents", headers=ADMIN_HEADERS).json()
    agent = body["agents"][0]
    assert set(agent) == {
        "agent_id", "role", "scope", "endpoint", "status",
        "registered_at", "last_seen_at", "last_seen_source",
        "key_count", "key_ref",
    }
    assert agent["agent_id"] == "hermes"
    assert agent["role"] == "agent"
    assert agent["scope"] == ["projects"]
    assert agent["status"] == "active"
    assert agent["registered_at"] is None  # S1 增强未启用 → 恒 null
    assert agent["key_count"] == 1
    assert set(body) == {"agents", "count", "generated_at", "snapshot_at", "source"}


def test_agents_aggregates_multiple_keys_per_agent(client):
    """同一 agent_id 多把 Key → 只有一条，key_count==2，scope 取并集。

    HTTP 注册端点已做去重（重复 id → 409），故第 2 把 Key 直接走 store 签发，
    仍验证「同 id 多 Key 聚合」这一 store/列表层行为不变。
    """
    client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": ["projects"]},
        headers=ADMIN_HEADERS,
    )
    client.app.state.key_store.register_agent("hermes", ["memory"])
    body = client.get("/v1/admin/agents", headers=ADMIN_HEADERS).json()
    assert body["count"] == 1
    agent = body["agents"][0]
    assert agent["key_count"] == 2
    assert sorted(agent["scope"]) == ["memory", "projects"]


def test_agents_role_filter(client):
    """role 过滤：不匹配返回空列表而非 404。"""
    client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": []},
        headers=ADMIN_HEADERS,
    )
    hit = client.get("/v1/admin/agents?role=agent", headers=ADMIN_HEADERS).json()
    assert hit["count"] == 1

    miss = client.get("/v1/admin/agents?role=guardian", headers=ADMIN_HEADERS)
    assert miss.status_code == 200
    assert miss.json()["agents"] == []
    assert miss.json()["count"] == 0


def test_agents_active_within_sec_filter(client, conns):
    """active_within_sec：只留窗口内活跃的；last_seen_at=null 的被剔除。"""
    _, session_conn, _ = conns
    for aid in ("fresh", "stale", "never"):
        client.post(
            "/v1/admin/agents/register",
            json={"agent_id": aid, "scope": []},
            headers=ADMIN_HEADERS,
        )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _insert_raw(session_conn, "f1", "fresh", _iso(now - timedelta(seconds=30)))
    _insert_raw(session_conn, "f2", "stale", _iso(now - timedelta(days=3)))

    body = client.get(
        "/v1/admin/agents?active_within_sec=3600", headers=ADMIN_HEADERS
    ).json()
    assert [a["agent_id"] for a in body["agents"]] == ["fresh"]
    assert body["count"] == 1


@pytest.mark.parametrize("bad", ["-1", "abc", "1.5"])
def test_agents_invalid_active_within_sec_returns_400(client, bad):
    """非法 active_within_sec（负数 / 非整数）→ 400 ERR_INVALID_ARGS。"""
    resp = client.get(f"/v1/admin/agents?active_within_sec={bad}", headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_agents_is_read_only(client):
    """只读：连续两次调用结果一致，且不影响 register/revoke 行为。"""
    client.post(
        "/v1/admin/agents/register",
        json={"agent_id": "hermes", "scope": []},
        headers=ADMIN_HEADERS,
    )
    first = client.get("/v1/admin/agents", headers=ADMIN_HEADERS).json()
    second = client.get("/v1/admin/agents", headers=ADMIN_HEADERS).json()
    assert first["agents"] == second["agents"]
    assert first["count"] == second["count"] == 1

    revoked = client.delete("/v1/admin/agents/hermes", headers=ADMIN_HEADERS)
    assert revoked.status_code == 200, revoked.text
    after = client.get("/v1/admin/agents", headers=ADMIN_HEADERS).json()
    assert after["count"] == 0
