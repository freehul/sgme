"""ST-35 T-101：人格洞察 HTTP 端点测试（fixture 对齐 test_care.py 模式）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme.config import load_config as sgme_load_config
from sgme.data import db as db_mod
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    yield create_app(
        cfg=sgme_load_config(),
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


class TestPersonaEndpoints:
    def test_traits_empty(self, client):
        r = client.get("/v1/admin/persona/traits", headers=AGENT_HEADERS)
        assert r.status_code == 200
        assert r.json() == {"traits": [], "count": 0}

    def test_mbti_add_and_get(self, client):
        ok = client.post(
            "/v1/admin/persona/mbti",
            json={"mbti_type": "INFJ", "note": "自报"},
            headers=AGENT_HEADERS,
        )
        assert ok.status_code == 200 and ok.json()["record"]["mbti_type"] == "INFJ"
        bad = client.post(
            "/v1/admin/persona/mbti", json={"mbti_type": "XYZ!"},
            headers=AGENT_HEADERS,
        )
        assert bad.status_code == 400
        got = client.get("/v1/admin/persona/mbti", headers=AGENT_HEADERS)
        assert got.status_code == 200
        body = got.json()
        assert body["latest"]["mbti_type"] == "INFJ"
        assert len(body["history"]) == 1

    def test_reports_empty_and_404(self, client):
        r = client.get("/v1/admin/persona/reports", headers=AGENT_HEADERS)
        assert r.status_code == 200 and r.json()["count"] == 0
        r2 = client.get("/v1/admin/persona/reports/nope", headers=AGENT_HEADERS)
        assert r2.status_code == 404

    def test_auth_required(self, client):
        r = client.get("/v1/admin/persona/traits")
        assert r.status_code in (401, 403)

    def test_calibrate_conflict_lock(self, client, monkeypatch):
        """执行锁被占 → 409。"""
        from sgme.engine import persona_monthly
        assert persona_monthly.RUN_LOCK.acquire(blocking=False)
        try:
            r = client.post("/v1/admin/persona/calibrate", headers=AGENT_HEADERS)
            assert r.status_code == 409
        finally:
            persona_monthly.RUN_LOCK.release()
