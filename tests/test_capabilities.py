# -*- coding: utf-8 -*-
"""tests/test_capabilities.py：D-58 capabilities 广告 + D-64 409/410 处置回归。

判定口径：
- GET /v1/capabilities（Agent Key）：三字段（schema_version/api_compat/catalog_version）
  + 兼容期窗口 + 端点清单；无 key 拒绝；
- 默认配置下兼容守卫**零行为变更**（冲突端点不返回 409/410）；
- 显式开启 enforce 后：未声明 2.0 的客户端 → 409 + upgrade_hint；声明 2 → 放行；
- 开启 enforce_gone 后：场景层下线端点 → 410；
- 纯函数 classify/enforcement 的分支矩阵。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import compat as compat_mod
from sgme import config as sgme_config
from sgme.data import db as db_mod

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


def _make_app(tmp_path, monkeypatch, compat_cfg=None):
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", raw)

    cfg = sgme_config.load_config()
    cfg["skills"] = {"enabled": False, "source_dirs": []}
    if compat_cfg is not None:
        cfg["compat"] = {**cfg.get("compat", {}), **compat_cfg}

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
        start_background_tasks=False,
    )
    return app, (mem_conn, session_conn, wiki_conn)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    app, conns = _make_app(tmp_path, monkeypatch)
    yield app
    for c in conns:
        if c is not None:
            c.close()


@pytest.fixture()
def client(app):
    return TestClient(app)


# ---------- 纯函数矩阵 ----------


def test_classify_conflict_and_gone():
    assert compat_mod.classify("POST", "/v1/admin/prompts/publish")["kind"] == "conflict"
    assert compat_mod.classify("PUT", "/v1/admin/skills/foo")["kind"] == "conflict"
    assert compat_mod.classify("POST", "/v1/admin/skills/reindex")["kind"] == "conflict"
    assert compat_mod.classify("POST", "/v1/admin/scenes/x/status")["kind"] == "gone"
    assert compat_mod.classify("POST", "/v1/admin/scene-gc/trigger")["kind"] == "gone"
    # 豁免端点不进清单
    assert compat_mod.classify("POST", "/v1/append") is None
    assert compat_mod.classify("POST", "/v1/admin/refine/trigger") is None
    assert compat_mod.classify("GET", "/v1/admin/skills/foo") is None


def test_enforcement_decision_matrix():
    off = {"compat": {"enforce": False, "enforce_gone": False}}
    assert compat_mod.enforcement_decision("PUT", "/v1/admin/skills/x", {}, off) is None

    on = {"compat": {"enforce": True, "enforce_gone": False}}
    d = compat_mod.enforcement_decision("PUT", "/v1/admin/skills/x", {}, on)
    assert d and d["status"] == 409 and "upgrade_hint" in d["body"]
    legacy = compat_mod.enforcement_decision(
        "PUT", "/v1/admin/skills/x", {"X-SGME-API-Version": "1"}, on
    )
    assert legacy and legacy["status"] == 409
    v2 = compat_mod.enforcement_decision(
        "PUT", "/v1/admin/skills/x", {"X-SGME-API-Version": "2"}, on
    )
    assert v2 is None

    gone = {"compat": {"enforce": False, "enforce_gone": True}}
    dg = compat_mod.enforcement_decision("POST", "/v1/admin/scene-gc/trigger", {}, gone)
    assert dg and dg["status"] == 410
    # gone 不受客户端版本影响
    assert compat_mod.enforcement_decision(
        "POST", "/v1/admin/scene-gc/trigger", {"X-SGME-API-Version": "2"}, gone
    )["status"] == 410


def test_compat_phase_pre_stable_and_window():
    phase = compat_mod.compat_phase({"compat": {"stable_since": "", "window_days": 90}})
    assert phase["phase"] == "pre_stable" and phase["ends_at"] is None
    phase2 = compat_mod.compat_phase(
        {"compat": {"stable_since": "2026-01-01", "window_days": 90}}
    )
    assert phase2["phase"] == "expired"
    assert phase2["ends_at"] == "2026-04-01"


# ---------- 端点 ----------


def test_capabilities_endpoint(client):
    resp = client.get("/v1/capabilities", headers=AGENT_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == db_mod.SCHEMA_VERSION
    assert body["api_compat"] == "1.x"
    assert body["catalog_version"] == "disabled", "技能模块禁用时应如实标注"
    assert body["compat"]["phase"] == "pre_stable"
    assert body["server"]["name"] == "sgme"
    assert len(body["endpoints"]["conflict"]) >= 5
    assert len(body["endpoints"]["gone"]) == 2
    assert body["enforcement"] == {"conflict": False, "gone": False}


def test_capabilities_requires_key(client):
    resp = client.get("/v1/capabilities")
    assert resp.status_code in (401, 403), resp.text


def test_default_app_does_not_enforce(tmp_path, monkeypatch):
    app, conns = _make_app(tmp_path, monkeypatch)
    try:
        c = TestClient(app)
        r = c.post("/v1/admin/prompts/publish", headers=ADMIN_HEADERS, json={})
        assert r.status_code != 409
        r2 = c.post("/v1/admin/scene-gc/trigger", headers=ADMIN_HEADERS)
        assert r2.status_code != 410
    finally:
        for x in conns:
            if x is not None:
                x.close()


def test_enforce_conflict_returns_409(tmp_path, monkeypatch):
    app, conns = _make_app(tmp_path, monkeypatch, compat_cfg={"enforce": True})
    try:
        c = TestClient(app)
        r = c.post("/v1/admin/prompts/publish", headers=ADMIN_HEADERS, json={})
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["error"] == "semantics_changed"
        assert body["capabilities"] == "/v1/capabilities"
        # 声明 2.0 的客户端放行（不返回 409；后面鉴权/校验错误照常）
        r2 = c.post(
            "/v1/admin/prompts/publish",
            headers={**ADMIN_HEADERS, "X-SGME-API-Version": "2"},
            json={},
        )
        assert r2.status_code != 409
    finally:
        for x in conns:
            if x is not None:
                x.close()


def test_enforce_gone_returns_410(tmp_path, monkeypatch):
    app, conns = _make_app(tmp_path, monkeypatch, compat_cfg={"enforce_gone": True})
    try:
        c = TestClient(app)
        r = c.post("/v1/admin/scene-gc/trigger", headers=ADMIN_HEADERS)
        assert r.status_code == 410, r.text
        assert r.json()["error"] == "endpoint_gone"
    finally:
        for x in conns:
            if x is not None:
                x.close()
