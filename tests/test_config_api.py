"""配置管理 API 测试（/v1/admin/config）。"""
from __future__ import annotations

import json

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
    """隔离 raw/ 目录。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir):
    """创建隔离的 FastAPI 应用（tmp data/ + raw/ + 配置落盘隔离）。

    配置落盘路径经 SGME_CONFIG_PATH 指向 tmp_path/sgme_test.yaml，
    防止 /v1/admin/config 的 PUT/POST 写回真实 config/sgme.yaml（防污染护栏）。
    """
    from sgme.profile import tier0 as tier0_mod

    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")
    # 配置落盘隔离：所有 /v1/admin/config 写操作落到 tmp，不碰真实配置
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))

    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    from sgme.server.app import create_app

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


def test_get_config_returns_sections(app, client):
    """GET /v1/admin/config 返回可配置段。"""
    resp = client.get("/v1/admin/config", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "config" in body
    for s in ("l1", "l2", "refine", "search", "backup"):
        assert s in body["config"], f"缺配置段 {s}"
    assert body["config"]["refine"]["refine_on_append"] is False
    # chunk_size 以配置文件实际值为准（甜点区定稿 5000），不写死 8000 默认值
    assert body["config"]["l1"]["chunk_size"] == sgme_config.load_config()["l1"]["chunk_size"]


def test_get_config_requires_admin(app, client):
    """Agent Key 调配置接口 → 403。"""
    resp = client.get("/v1/admin/config", headers=AGENT_HEADERS)
    assert resp.status_code == 403


def test_get_config_section(app, client):
    """GET /v1/admin/config/refine 返回单段。"""
    resp = client.get("/v1/admin/config/refine", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["section"] == "refine"
    assert "batch_scan" in body["config"]


def test_get_config_section_unknown(app, client):
    """未知段 → 404。"""
    resp = client.get("/v1/admin/config/llm", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


def test_update_config_section(app, client, tmp_path):
    """PUT 更新 refine 段 → 生效 + 落盘（隔离路径由 app fixture 统一设置）。"""
    cfg_path = tmp_path / "sgme_test.yaml"
    resp = client.put("/v1/admin/config", json={
        "section": "refine",
        "values": {"refine_on_append": True, "batch_scan": {"interval_min": 5}},
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"]["refine_on_append"] is True
    assert body["config"]["batch_scan"]["interval_min"] == 5
    # 生效（app.state.cfg 已更新）
    assert app.state.cfg["refine"]["refine_on_append"] is True
    # 落盘（隔离文件含新值）
    import yaml
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["refine"]["refine_on_append"] is True
    # 还原
    client.put("/v1/admin/config", json={
        "section": "refine",
        "values": {"refine_on_append": False, "batch_scan": {"interval_min": 10}},
    }, headers=ADMIN_HEADERS)


def test_update_config_multi_section(app, client):
    """无 section 形态：values 键 = 段名，多段更新。"""
    resp = client.put("/v1/admin/config", json={
        "values": {
            "l1": {"chunk_size": 10000},
            "refine": {"refine_on_append": True},
        },
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"]["l1"]["chunk_size"] == 10000
    assert body["config"]["refine"]["refine_on_append"] is True
    # 还原
    client.put("/v1/admin/config", json={
        "values": {
            "l1": {"chunk_size": 8000},
            "refine": {"refine_on_append": False},
        },
    }, headers=ADMIN_HEADERS)


def test_update_config_unknown_section(app, client):
    """未知段更新 → 404。"""
    resp = client.put("/v1/admin/config", json={
        "section": "llm",
        "values": {"foo": 1},
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 404


def test_update_config_requires_admin(app, client):
    """Agent Key 更新 → 403。"""
    resp = client.put("/v1/admin/config", json={
        "section": "refine", "values": {"refine_on_append": True},
    }, headers=AGENT_HEADERS)
    assert resp.status_code == 403


def test_config_persist_does_not_pollute_repo_config(app, client):
    """回归护栏：/v1/admin/config 的落盘不得改动项目根真实 config/sgme.yaml。

    隔离由 app fixture 经 SGME_CONFIG_PATH 实现。本测试在调用前后对真实
    config/sgme.yaml 做 sha256 比对，断言完全相等。临时 unset 隔离可验证其会变红。
    """
    import hashlib

    # 项目根真实配置（SGME_HOME 隔离下 DEFAULT_SGME_CONFIG 已重定向，须显式指项目根）
    repo_cfg = sgme_config.PROJECT_ROOT / "config" / "sgme.yaml"
    assert repo_cfg.exists(), f"真实配置文件缺失: {repo_cfg}"
    before = hashlib.sha256(repo_cfg.read_bytes()).hexdigest()
    # 多段更新（含 backup.dir），会触发 _persist；若隔离失效将写回真实配置
    resp = client.put("/v1/admin/config", json={
        "values": {
            "l1": {"chunk_size": 10000},
            "refine": {"refine_on_append": True},
            "backup": {"dir": "C:\\evil\\polluted\\backups"},
        },
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    after = hashlib.sha256(repo_cfg.read_bytes()).hexdigest()
    assert before == after, "真实 config/sgme.yaml 被测试落盘污染"
