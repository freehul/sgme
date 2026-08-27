"""tests/test_update_check_endpoint.py：ST-34 扩展「检查更新」端点测试。

覆盖 POST /v1/admin/update/check：
- 鉴权：缺 Key → 403
- 正常：强制刷新版本检测缓存，返回 {update_available, latest_version, update_checked_at, update_error}
- 检测失败降级：update_error 回填、update_available=False，仍 200（永不抛异常）
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.operations import update_check
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    from sgme.data import db as db_mod
    from sgme.data import memory_dao

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def no_bearer(monkeypatch):
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
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def test_update_check_requires_admin_key(client):
    r = client.post("/v1/admin/update/check")
    assert r.status_code == 403


def test_update_check_returns_detection(client, monkeypatch):
    """强制刷新命中新版本 → 返回 update_available=True + latest_version。"""
    canned = {
        "update_available": True,
        "latest_version": "v1.0.2",
        "update_checked_at": "2026-08-27T00:00:00+00:00",
        "update_error": None,
    }
    monkeypatch.setattr(update_check, "refresh", lambda current, cfg=None: canned)

    r = client.post("/v1/admin/update/check", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["update_available"] is True
    assert body["latest_version"] == "v1.0.2"
    assert body["update_checked_at"] == "2026-08-27T00:00:00+00:00"
    assert body["update_error"] is None


def test_update_check_degrade_on_error(client, monkeypatch):
    """检测失败 → update_error 回填、update_available=False，端点仍 200 不抛。"""
    canned = {
        "update_available": False,
        "latest_version": "1.0.1",
        "update_checked_at": "2026-08-27T00:00:00+00:00",
        "update_error": "release 响应缺 tag_name",
    }
    monkeypatch.setattr(update_check, "refresh", lambda current, cfg=None: canned)

    r = client.post("/v1/admin/update/check", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["update_available"] is False
    assert body["update_error"] == "release 响应缺 tag_name"
