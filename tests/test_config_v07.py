"""tests/test_config_v07.py：v0.7 阶段 4 配置扩展测试。

覆盖：
1. load_config 返回 wiki/skills_hub/logging sections（sgme.yaml 缺省兜底）
2. CONFIG_SECTIONS 扩展（可写段）
3. wiki.enabled=false 时不挂载 /v1/wiki/* 路由；true 时挂载
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.server.app import create_app
from sgme.data import db as db_mod


def test_load_config_has_v07_sections(tmp_path, monkeypatch):
    # 隔离配置路径（防写回真实 sgme.yaml）
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = sgme_config.load_config()
    assert cfg["wiki"] == {"enabled": True} or cfg["wiki"].get("enabled") is True
    # skills_hub 段存在即可（值随 config/sgme.yaml 生产配置漂移，
    # 0.8 ST-11 已启用 copy 模式——2026-08-10 修正：不断言具体值）
    assert "enabled" in cfg["skills_hub"]
    assert "mode" in cfg["skills_hub"]
    assert cfg["logging"]["level"] == "INFO"
    assert cfg["logging"]["format"] == "console"


def test_config_sections_extended():
    assert {"wiki", "skills_hub", "logging"} <= sgme_config.CONFIG_SECTIONS


@pytest.fixture
def base_app(tmp_path):
    mem = db_mod.connect_memory(tmp_path / "data")
    session = db_mod.connect_session(tmp_path / "data")
    wiki = db_mod.connect_wiki(tmp_path / "data")
    yield mem, session, wiki
    db_mod.close(mem)
    db_mod.close(session)
    db_mod.close(wiki)


def test_wiki_routes_mounted_when_enabled(base_app, tmp_path):
    mem, session, wiki = base_app
    app = create_app(
        cfg={"wiki": {"enabled": True}, "dimensions": {}, "aliases": {},
             "paths": {"data_dir": str(tmp_path / "data")}},
        mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin", agent_key="test-agent",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
    )
    c = TestClient(app)
    r = c.get("/v1/wiki/pages", headers={"X-API-Key": "test-agent"})
    assert r.status_code == 200


def test_wiki_routes_unmounted_when_disabled(base_app, tmp_path):
    mem, session, wiki = base_app
    app = create_app(
        cfg={"wiki": {"enabled": False}, "dimensions": {}, "aliases": {},
             "paths": {"data_dir": str(tmp_path / "data")}},
        mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin", agent_key="test-agent",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
    )
    c = TestClient(app)
    r = c.get("/v1/wiki/pages", headers={"X-API-Key": "test-agent"})
    assert r.status_code == 404
