"""tests/test_routes_scenes_offline.py：场景层写端点下线（9.6 / D-53，2026-11-05）。

口径：
1. POST /v1/admin/scenes/{scene_id}/status 路由**已摘除** → 404（或 405，取决于
   Starlette 的路径匹配细节）；未开 compat.enforce_gone 时不做 410 拦截，
   旧客户端拿到的是「端点不存在」而不是「已下线」——故同时断言 compat 守卫：
   开 enforce_gone 后同一请求返回 410 endpoint_gone + upgrade_hint。
2. GET /v1/admin/scenes 只读列表保留（场景数据四表冻结，不删行）。

用 WebUI 同款 admin key 调用，确保鉴权档位没被顺手降级。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
ADMIN = {"X-API-Key": ADMIN_KEY}

SCENE_STATUS_URL = "/v1/admin/scenes/scene_x/status"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SGME_HOME", str(tmp_path))
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", raw)

    cfg = sgme_config.load_config()
    cfg["skills"] = {"enabled": False, "source_dirs": []}
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture()
def client(app):
    return TestClient(app)


def test_scene_status_route_removed(client):
    """POST 场景状态 → 路由不存在（404/405），不是 200 静默改状态。"""
    resp = client.post(SCENE_STATUS_URL, json={"status": "rejected"}, headers=ADMIN)
    assert resp.status_code in (404, 405), resp.text


def test_scene_status_route_removed_even_without_key(client):
    """路由摘除后鉴权不再参与：无 Key 也是 404/405（不会先回 403 再暴露存在性）。"""
    resp = client.post(SCENE_STATUS_URL, json={"status": "active"})
    assert resp.status_code in (404, 405), resp.text


def test_scene_status_405_on_wrong_method_shape(client):
    """同路径改用 GET 也应 404/405——确认整条路径没有残留处理函数。"""
    resp = client.get(SCENE_STATUS_URL, headers=ADMIN)
    assert resp.status_code in (404, 405), resp.text


def test_scene_status_db_row_untouched(client, app):
    """端点摘除后这条路径不可能再写库：场景行状态保持原值。"""
    from sgme.data.db import SCENE_RETIRED_DDL

    mem_conn = app.state.mem_conn
    mem_conn.executescript(SCENE_RETIRED_DDL)
    mem_conn.execute(
        "INSERT INTO scenes (scene_id, title, content, created_at, updated_at) "
        "VALUES ('scene_x', 't', 'c', '2026-11-20T00:00:00Z', '2026-11-20T00:00:00Z')"
    )
    mem_conn.commit()
    client.post(SCENE_STATUS_URL, json={"status": "archived"}, headers=ADMIN)
    row = mem_conn.execute(
        "SELECT status FROM scenes WHERE scene_id='scene_x'"
    ).fetchone()
    assert row["status"] == "active"


def test_compat_gone_returns_410_when_enforced(client, app):
    """开 compat.enforce_gone 后同一请求 → 410 endpoint_gone（旧客户端拿到明确下线提示）。"""
    app.state.cfg["compat"] = {**(app.state.cfg.get("compat") or {}), "enforce_gone": True}
    resp = client.post(SCENE_STATUS_URL, json={"status": "rejected"}, headers=ADMIN)
    assert resp.status_code == 410
    body = resp.json()
    assert body["error"] == "endpoint_gone"
    assert body["upgrade_hint"]


def test_scene_list_kept_read_only(client):
    """GET /v1/admin/scenes 只读列表保留（场景表缺失也返回 200 空列表）。"""
    resp = client.get("/v1/admin/scenes", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    assert body["total"] == 0


def test_scene_list_reads_retired_tables(client, app):
    """四表 rename `_retired` 后只读端点仍 200 且能读到冻结数据（T-427）。"""
    mem_conn = app.state.mem_conn
    mem_conn.executescript(
        """
        CREATE TABLE scenes_retired (
          scene_id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
          heat INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          last_memory_added_at TEXT, content_seg TEXT);
        CREATE TABLE scene_memories_retired (
          scene_id TEXT NOT NULL, memory_id TEXT NOT NULL,
          PRIMARY KEY (scene_id, memory_id));
        """
    )
    mem_conn.execute(
        "INSERT INTO scenes_retired (scene_id, title, content, heat, status, "
        "created_at, updated_at) VALUES "
        "('scene_r', '冻结场景', '历史正文', 7, 'active', "
        "'2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    mem_conn.commit()
    resp = client.get("/v1/admin/scenes", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["scene_id"] == "scene_r"
    assert body["items"][0]["heat"] == 7
