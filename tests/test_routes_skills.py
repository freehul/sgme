"""tests/test_routes_skills.py：技能仓库 CRUD 端点测试。

覆盖：
1. ``GET /v1/admin/skills`` 返回列表 / 基础信息（enabled/mode/path）
2. ``PUT /v1/admin/skills/{name}`` 写入技能
3. ``GET /v1/admin/skills/{name}`` 读取技能全文
4. ``DELETE /v1/admin/skills/{name}`` 删除技能（幂等）
5. 鉴权：agent key → 403
6. skills_hub 未启用 → 400
7. 健壮性：名字非法 / 内容为空 → 400；不存在 → 404

零真实网络：全部走本地临时目录，不触发 git/远端。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}
ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    from sgme import config as _c
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(_c, "RAW_DIR", raw)
    return raw


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir):
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))

    cfg = sgme_config.load_config()
    # 开启技能仓库（map 模式指向临时目录）
    cfg["skills_hub"] = {
        "enabled": True,
        "mode": "map",
        "path": str(tmp_path / "skills"),
    }

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 鉴权 ----------

def test_skills_requires_admin(app, client):
    resp = client.get("/v1/admin/skills", headers=AGENT_HEADERS)
    assert resp.status_code == 403


# ---------- 列表 / 基础信息 ----------

def test_skills_list_empty(app, client):
    resp = client.get("/v1/admin/skills", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["mode"] == "map"
    assert data["total"] == 0
    assert data["skills"] == []


# ---------- 写入 / 读取 / 删除 ----------

def test_skills_crud_flow(app, client):
    # 写入
    resp = client.put(
        "/v1/admin/skills/my-skill",
        headers=ADMIN_HEADERS,
        json={"content": "# 我的技能\n\n你好，世界。"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "my-skill"

    # 列表出现
    resp = client.get("/v1/admin/skills", headers=ADMIN_HEADERS)
    data = resp.json()
    assert data["total"] == 1
    assert data["skills"] == ["my-skill"]

    # 读取原文
    resp = client.get("/v1/admin/skills/my-skill", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert "你好，世界。" in resp.json()["content"]

    # 删除
    resp = client.delete("/v1/admin/skills/my-skill", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # 删除后列表为空
    resp = client.get("/v1/admin/skills", headers=ADMIN_HEADERS)
    assert resp.json()["total"] == 0


def test_skills_delete_idempotent(app, client):
    resp = client.delete("/v1/admin/skills/ghost", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is False


# ---------- 健壮性 ----------

def test_skills_get_missing_returns_404(app, client):
    resp = client.get("/v1/admin/skills/ghost", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


def test_skills_put_invalid_name(app, client):
    # 含空格的技能名不过白名单（字母/数字/下划线/中划线/点）
    resp = client.put(
        "/v1/admin/skills/bad name",
        headers=ADMIN_HEADERS,
        json={"content": "x"},
    )
    assert resp.status_code == 400


def test_skills_put_empty_content(app, client):
    resp = client.put(
        "/v1/admin/skills/ok",
        headers=ADMIN_HEADERS,
        json={"content": "   "},
    )
    assert resp.status_code == 400


# ---------- 未启用 ----------

def test_skills_disabled_returns_400(app_disabled, client_disabled):
    resp = client_disabled.get("/v1/admin/skills", headers=ADMIN_HEADERS)
    assert resp.status_code == 400


@pytest.fixture
def app_disabled(tmp_path, monkeypatch, raw_dir):
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))

    cfg = sgme_config.load_config()
    cfg["skills_hub"] = {"enabled": False}

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client_disabled(app_disabled):
    return TestClient(app_disabled)