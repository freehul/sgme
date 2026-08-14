"""维度注册表管理 API 测试（/v1/admin/registry，契约 §5）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.raw import store as raw_store
from sgme.data import db as db_mod
from sgme.data import memory_dao

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir):
    from sgme.profile import tier0 as tier0_mod

    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    from sgme.server.app import create_app

    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


def test_registry_list_dimensions(app, client):
    """GET /v1/admin/registry 返回全部维度 + 别名。"""
    resp = client.get("/v1/admin/registry", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == len(sgme_config.load_config()["dimensions"])
    dims = {d["id"] for d in body["dimensions"]}
    assert {"identity", "family", "projects", "tech_stack"} <= dims
    # 每个维度带别名
    identity = next(d for d in body["dimensions"] if d["id"] == "identity")
    assert "身份" in identity["aliases"]


def test_registry_get_single_dimension(app, client):
    """GET /v1/admin/registry/{dim_id} 单维度详情。"""
    resp = client.get("/v1/admin/registry/identity", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["dimension"]["id"] == "identity"
    assert body["dimension"]["display_name"] == "身份"


def test_registry_get_unknown_dimension(app, client):
    """未知维度 → 404。"""
    resp = client.get("/v1/admin/registry/nonexistent", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


def test_registry_add_dimension(app, client):
    """POST /v1/admin/registry/dimensions 新增维度（幂等）。"""
    payload = {
        "id": "testdim",
        "display_name": "测试维度",
        "category": "偏好",
        "time_velocity": "static",
        "description": "审计测试新增",
    }
    resp = client.post("/v1/admin/registry/dimensions", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    # 幂等：重复提交不报错
    resp2 = client.post("/v1/admin/registry/dimensions", json=payload, headers=ADMIN_HEADERS)
    assert resp2.status_code == 200
    # 出现在列表
    resp3 = client.get("/v1/admin/registry", headers=ADMIN_HEADERS)
    ids = [d["id"] for d in resp3.json()["dimensions"]]
    assert "testdim" in ids


def test_registry_add_dimension_invalid_id(app, client):
    """非法维度 id（非 snake_case）→ 400。"""
    resp = client.post("/v1/admin/registry/dimensions", json={
        "id": "Bad Dim!", "display_name": "坏维度",
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 400


def test_registry_set_dimension_active(app, client):
    """PUT /v1/admin/registry/dimensions/{dim_id} 停用/启用维度。"""
    # 先新增
    client.post("/v1/admin/registry/dimensions", json={
        "id": "tempdim", "display_name": "临时维度", "category": "动态",
    }, headers=ADMIN_HEADERS)
    # 停用
    resp = client.put("/v1/admin/registry/dimensions/tempdim", json={"active": False},
                      headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    # 列表（默认只列 active）不含它；active_only=false 含它
    resp_list = client.get("/v1/admin/registry", headers=ADMIN_HEADERS)
    assert "tempdim" not in [d["id"] for d in resp_list.json()["dimensions"]]
    resp_all = client.get("/v1/admin/registry?active_only=false", headers=ADMIN_HEADERS)
    assert "tempdim" in [d["id"] for d in resp_all.json()["dimensions"]]


def test_registry_add_alias(app, client):
    """POST /v1/admin/registry/aliases 新增别名。"""
    resp = client.post("/v1/admin/registry/aliases", json={
        "alias": "技术选型", "dimension_id": "tech_stack",
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    # 别名出现在维度详情
    resp2 = client.get("/v1/admin/registry/tech_stack", headers=ADMIN_HEADERS)
    assert "技术选型" in resp2.json()["dimension"]["aliases"]


def test_registry_remove_alias(app, client):
    """DELETE /v1/admin/registry/aliases/{alias} 删除别名。"""
    # 先加一个
    client.post("/v1/admin/registry/aliases", json={
        "alias": "临时别名", "dimension_id": "identity",
    }, headers=ADMIN_HEADERS)
    resp = client.delete("/v1/admin/registry/aliases/临时别名", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    resp2 = client.get("/v1/admin/registry/identity", headers=ADMIN_HEADERS)
    assert "临时别名" not in resp2.json()["dimension"]["aliases"]


def test_registry_requires_admin(app, client):
    """Agent Key 调 registry → 403。"""
    for method, path, kwargs in [
        ("get", "/v1/admin/registry", {}),
        ("post", "/v1/admin/registry/dimensions", {"json": {"id": "x", "display_name": "x"}}),
    ]:
        resp = getattr(client, method)(path, headers=AGENT_HEADERS, **kwargs)
        assert resp.status_code == 403
