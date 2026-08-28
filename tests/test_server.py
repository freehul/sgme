"""T7 测试：HTTP 服务（FastAPI TestClient 全端点 + 鉴权 + 闭环）。

覆盖 checklist T7：
- 无 X-API-Key 调 /v1/inject → 403；带 Agent Key → 200
- Agent Key 调 /v1/admin/stats → 403；管理员 Key → 200
- /v1/append 幂等：同 session_key+started_at 重复调用不重复生成文件段
- /v1/search 返回结果带 trace（source_ref 指向 raw 文件）
- /v1/health 返回 refinement.watermark_age_sec
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.raw import store as raw_store
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)  # raw_store 用 config.RAW_DIR
    return rd


@pytest.fixture
def app(tmp_path, cfg, raw_dir, monkeypatch):
    """创建隔离的 FastAPI 应用（tmp_path data/ + raw/）。"""
    from sgme.profile import tier0 as tier0_mod

    # 隔离 tier0_summary.json（防真实 data/ 摘要污染测试）
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        # 隔离 agent key 存储：缺省会写/读真实 data/agent_keys.json，污染并暴露生产注册表
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    """TestClient（带 app 隔离）。"""
    return TestClient(app)


AGENT_HEADERS = {"X-API-Key": "test-agent-key"}
ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg_text(role: str, content: str, ts: str | None = None) -> str:
    ts = ts or _now_iso()
    return f"# {ts} {role}\n{content}\n"


# ---------- 鉴权 ----------

def test_inject_no_api_key_returns_403(client):
    """无 X-API-Key 调 /v1/inject → 403。"""
    resp = client.post("/v1/inject", json={"mode": "daily"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "ERR_FORBIDDEN"


def test_inject_with_agent_key_returns_200(client):
    """带 Agent Key → 200。"""
    resp = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "blocks" in body
    assert "stats" in body


def test_admin_stats_with_agent_key_returns_403(client):
    """Agent Key 调 /v1/admin/stats → 403。"""
    resp = client.get("/v1/admin/stats", headers=AGENT_HEADERS)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_admin_stats_with_admin_key_returns_200(client):
    """管理员 Key 调 /v1/admin/stats → 200。"""
    resp = client.get("/v1/admin/stats", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "memories" in body
    assert "raw_files" in body


def test_health_no_api_key_returns_200(client):
    """/v1/health 不强制 X-API-Key（Bearer 旁路关闭时）。"""
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.1.0"
    assert "refinement" in body
    assert "watermark_age_sec" in body["refinement"]


# ---------- /v1/append 幂等 ----------

def test_append_creates_new_file(client, raw_dir):
    """append 新会话 → 201 文件 + raw_files 行。"""
    content = _msg_text("user", "你好") + _msg_text("assistant", "你好，有什么可以帮你？")
    resp = client.post("/v1/append", json={
        "session_key": "test-session-1",
        "agent_id": "test-agent",
        "started_at": "2026-08-04T10:00:00Z",
        "ended_at": "2026-08-04T10:30:00Z",
        "content": content,
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "new"
    assert body["file_id"]
    # 文件存在
    assert (raw_dir / "sessions" / f"{body['file_id']}.md").exists()


def test_append_idempotent_same_session_and_started_at(client, raw_dir):
    """同 session_key + 同 started_at 重复调用 → 幂等（不重复生成文件段）。"""
    content = _msg_text("user", "你好")
    payload = {
        "session_key": "test-session-idem",
        "agent_id": "test-agent",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }
    r1 = client.post("/v1/append", json=payload, headers=AGENT_HEADERS)
    assert r1.status_code == 200
    file_id_1 = r1.json()["file_id"]

    r2 = client.post("/v1/append", json=payload, headers=AGENT_HEADERS)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["file_id"] == file_id_1
    assert body2.get("idempotent") is True


def test_append_without_agent_id_resolves_from_registered_key(client, raw_dir, app):
    """B35：body 不带 agent_id + 注册 agt_* key → 兜底落绑定 agent_id。"""
    import json

    # 注册一个绑定 agent_id 的 key
    r = client.post("/v1/admin/agents/register",
                    json={"agent_id": "planner", "scope": ["projects"]},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    reg_key = r.json()["api_key"]

    content = _msg_text("user", "注册 key 溯源兜底")
    resp = client.post("/v1/append", json={
        "session_key": "test-session-b35-1",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
        # 刻意不带 agent_id
    }, headers={"X-API-Key": reg_key})
    assert resp.status_code == 200, resp.text

    file_id = resp.json()["file_id"]
    conn: sqlite3.Connection = app.state.session_conn
    row = conn.execute("SELECT agent_id FROM raw_files WHERE file_id=?", (file_id,)).fetchone()
    assert row is not None and row[0] == "planner", f"预期兜底为 planner，实际 {row}"


def test_append_without_agent_id_default_from_env_key(client, raw_dir, app):
    """B35：body 不带 agent_id + env 主 agent key → 兜底落 default。"""
    content = _msg_text("user", "env key 溯源兜底")
    resp = client.post("/v1/append", json={
        "session_key": "test-session-b35-2",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 200, resp.text

    file_id = resp.json()["file_id"]
    conn: sqlite3.Connection = app.state.session_conn
    row = conn.execute("SELECT agent_id FROM raw_files WHERE file_id=?", (file_id,)).fetchone()
    assert row is not None and row[0] == "default", f"预期兜底为 default，实际 {row}"


def test_append_body_agent_id_wins_over_key_resolution(client, raw_dir, app):
    """B35：body 显式 agent_id 优先于 key 反查（显式 > 兜底）。"""
    import json

    r = client.post("/v1/admin/agents/register",
                    json={"agent_id": "planner", "scope": []},
                    headers=ADMIN_HEADERS)
    reg_key = r.json()["api_key"]

    content = _msg_text("user", "显式 agent_id 优先")
    resp = client.post("/v1/append", json={
        "session_key": "test-session-b35-3",
        "started_at": "2026-08-04T10:00:00Z",
        "agent_id": "explicit-agent",  # 显式传，应与 key 绑定的 planner 不同
        "content": content,
    }, headers={"X-API-Key": reg_key})
    assert resp.status_code == 200, resp.text

    file_id = resp.json()["file_id"]
    conn: sqlite3.Connection = app.state.session_conn
    row = conn.execute("SELECT agent_id FROM raw_files WHERE file_id=?", (file_id,)).fetchone()
    assert row is not None and row[0] == "explicit-agent", f"显式应优先，实际 {row}"


def test_append_different_started_at_appends(client, raw_dir):
    """同 session_key + 不同 started_at → 追加。"""
    c1 = _msg_text("user", "第一条")
    r1 = client.post("/v1/append", json={
        "session_key": "test-session-append",
        "started_at": "2026-08-04T10:00:00Z",
        "content": c1,
    }, headers=AGENT_HEADERS)
    file_id = r1.json()["file_id"]

    c2 = _msg_text("assistant", "第二条")
    r2 = client.post("/v1/append", json={
        "session_key": "test-session-append",
        "started_at": "2026-08-04T11:00:00Z",
        "content": c2,
    }, headers=AGENT_HEADERS)
    body2 = r2.json()
    assert body2["file_id"] == file_id
    assert body2.get("appended") is True
    assert body2["status"] == "new"


def test_append_invalid_content_returns_400(client):
    """content 无法解析出消息 → 400。"""
    resp = client.post("/v1/append", json={
        "session_key": "bad",
        "started_at": "2026-08-04T10:00:00Z",
        "content": "无格式文本",
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


# ---------- /v1/inject ----------

def test_inject_daily_returns_blocks_and_stats(client):
    """/v1/inject mode=daily → blocks + stats + tier0。"""
    resp = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "blocks" in body
    assert "stats" in body
    assert body["stats"]["mode"] == "daily"
    assert body["tier0"]["present"] is False


def test_inject_custom_filter(client):
    """/v1/inject custom_filter → 自定义查询。"""
    # 先插一条记忆
    app = client.app
    mem_conn = app.state.mem_conn
    memory_dao.insert_memory(
        mem_conn, content="测试项目", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["goals"],
    )
    resp = client.post("/v1/inject", json={
        "custom_filter": {"dimensions": ["goals"], "match": "any"},
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["blocks"]) >= 1


def test_inject_invalid_dimension_returns_400(client):
    """custom_filter 含未注册维度 → 400。"""
    resp = client.post("/v1/inject", json={
        "custom_filter": {"dimensions": ["nonexistent_dim"]},
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_inject_no_mode_no_filter_returns_400(client):
    """既无 mode 也无 custom_filter → 400。"""
    resp = client.post("/v1/inject", json={}, headers=AGENT_HEADERS)
    assert resp.status_code == 400


# ---------- /v1/search ----------

def test_search_returns_trace(client, raw_dir):
    """/v1/search 返回结果带 trace（source_ref 指向 raw 文件）。"""
    app = client.app
    mem_conn = app.state.mem_conn

    # 1. append 一个会话
    content = _msg_text("user", "SGME 底座从 Fork 改为 Python 自研")
    r = client.post("/v1/append", json={
        "session_key": "search-test",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }, headers=AGENT_HEADERS)
    file_id = r.json()["file_id"]

    # 2. 手动插一条带 source 的记忆（模拟提炼后落库）
    memory_dao.insert_memory(
        mem_conn, content="SGME 底座从 Fork 改为 Python 自研",
        memory_type="persona", priority=85, time_velocity="static",
        ttl_days=None, dimension_ids=["goals", "tech_stack"],
        sources=[(f"{file_id}:1", "session")],
    )

    # 3. search
    resp = client.post("/v1/search", json={
        "query": "SGME 底座",
        "scopes": ["memory"],
        "dimensions": ["goals"],
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) >= 1
    first = body["results"][0]
    assert first["source"] == "memory"
    assert "trace" in first
    assert len(first["trace"]) >= 1
    trace = first["trace"][0]
    assert trace["file_id"] == file_id
    assert trace["path"] is not None  # 指向 raw 文件


def test_search_empty_query_returns_empty(client):
    """空 query → 空结果。"""
    resp = client.post("/v1/search", json={"query": ""}, headers=AGENT_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_search_no_memory_scope_returns_empty(client):
    """scopes 不含 memory → 空结果。"""
    resp = client.post("/v1/search", json={
        "query": "x", "scopes": ["wiki_raw"],
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# ---------- /v1/memory/{id} ----------

def test_get_memory_returns_sources_and_archive_chain(client):
    """/v1/memory/{id} 返回 memory + sources + archive_chain。"""
    app = client.app
    mem_conn = app.state.mem_conn
    mid = memory_dao.insert_memory(
        mem_conn, content="测试记忆", memory_type="persona",
        priority=70, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
        sources=[("file-1:1", "session")],
    )
    resp = client.get(f"/v1/memory/{mid}", headers=AGENT_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["memory"]["memory_id"] == mid
    assert len(body["sources"]) >= 1
    assert "archive_chain" in body


def test_get_memory_not_found_returns_404(client):
    """不存在的 memory_id → 404。"""
    resp = client.get("/v1/memory/nonexistent-id", headers=AGENT_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"


# ---------- /v1/health 水位 ----------

def test_health_watermark_advances_after_refine(client, raw_dir, monkeypatch):
    """/v1/health watermark_age_sec：提炼后数值变小。

    通过 mock LLM + refine/trigger 触发提炼。
    """
    app = client.app
    mem_conn = app.state.mem_conn

    # append 一个会话
    content = _msg_text("user", "我是独立开发者")
    r = client.post("/v1/append", json={
        "session_key": "health-test",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }, headers=AGENT_HEADERS)
    file_id = r.json()["file_id"]

    # 健康检查：提炼前 queue_depth=1
    h1 = client.get("/v1/health").json()
    assert h1["refinement"]["queue_depth"] == 1
    assert h1["refinement"]["watermark_age_sec"] is None  # 还没提炼过

    # mock LLM 注入到 app.state（refine 会调 LLM）
    from sgme.engine import refine as refine_mod
    from sgme.engine import l1

    def fake_extract_l1(conversation, dimensions, llm_cfg, client=None, **kwargs):
        return ([{
            "content": "用户是独立开发者",
            "memory_type": "persona",
            "priority": 80,
            "time_velocity": "static",
            "dimensions": ["身份"],
            "source_message_ids": [1],
        }], "mock", {"stage": "l1_extraction", "version": "working-mock", "variant": None})

    monkeypatch.setattr(l1, "extract_l1", fake_extract_l1)

    # 触发提炼
    rt = client.post("/v1/admin/refine/trigger", json={
        "file_id": file_id,
    }, headers=ADMIN_HEADERS)
    assert rt.status_code == 200, rt.text
    assert rt.json()["status"] == "refined"

    # 健康检查：提炼后 watermark_age_sec 有值（小数值）
    h2 = client.get("/v1/health").json()
    assert h2["refinement"]["watermark_age_sec"] is not None
    assert h2["refinement"]["watermark_age_sec"] >= 0
    assert h2["refinement"]["queue_depth"] == 0  # 已提炼


# ---------- Admin 端点 ----------

def test_admin_register_agent(client):
    """/v1/admin/agents/register 签发新 Agent Key。"""
    resp = client.post("/v1/admin/agents/register", json={
        "agent_id": "new-agent",
        "scope": ["read"],
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key"].startswith("agt_")
    assert body["agent_id"] == "new-agent"

    # 新 key 可调非 admin 端点
    r2 = client.post("/v1/inject", json={"mode": "daily"},
                     headers={"X-API-Key": body["api_key"]})
    assert r2.status_code == 200


def test_admin_register_agent_with_agent_key_403(client):
    """Agent Key 调 register → 403。"""
    resp = client.post("/v1/admin/agents/register", json={
        "agent_id": "x",
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 403


def test_admin_refine_trigger_batch_empty(client):
    """refine/trigger batch 无 new 文件 → processed=0。"""
    resp = client.post("/v1/admin/refine/trigger", json={}, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["triggered"] == "batch"
    assert body["processed"] == 0


# ---------- 统一错误结构 ----------

def test_error_response_structure(client):
    """错误响应结构：{"error":{"code","message"}}。"""
    resp = client.post("/v1/inject", json={})
    assert resp.status_code == 403
    body = resp.json()
    assert "error" in body
    assert "code" in body["error"]
    assert "message" in body["error"]


# ---------- Bearer 鉴权 ----------

def test_bearer_token_enforced(tmp_path, cfg, raw_dir):
    """开启 Bearer 后，无 Bearer → 401；带正确 Bearer → 200。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="a", agent_key="b",
        bearer_token="secret-bearer",
    )
    try:
        c = TestClient(app)
        # 无 Bearer → 401
        r1 = c.get("/v1/health")
        assert r1.status_code == 401
        # 错误 Bearer → 401
        r2 = c.get("/v1/health", headers={"Authorization": "Bearer wrong"})
        assert r2.status_code == 401
        # 正确 Bearer → 200
        r3 = c.get("/v1/health", headers={"Authorization": "Bearer secret-bearer"})
        assert r3.status_code == 200
    finally:
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        db_mod.close(wiki_conn)
