"""运维可观测性端点测试（routes_ops）：TTL 处置看板与 LLM 成本看板的取数。

关键边界：两张审计表都是**懒建**的（首次产生记录才建表），
故「表不存在」必须返回 200 + 空集，而不是 404/500——否则新库刚上线时
看板直接报错。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))
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
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    yield app
    app.state.mem_conn.close()
    session_conn.close()
    if wiki_conn is not None:
        wiki_conn.close()


@pytest.fixture()
def client(app):
    return TestClient(app)


def test_ttl_disposition_empty_when_table_missing(client, app):
    """审计表未创建 → 200 + 空 items + 零 total（不报错）。

    2026-11-04：ttl_disposition 已进 MEMORY_DDL 正式建表（迁移阶段 1），
    所以必须显式 DROP 才能重现「老库没这张表」的状态。
    """
    app.state.mem_conn.execute("DROP TABLE IF EXISTS ttl_disposition")
    app.state.mem_conn.commit()
    r = client.get("/v1/admin/ops/ttl-disposition", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == {
        "valid_expired": 0, "renewed": 0, "anchor": 0, "sleeping": 0, "archived": 0
    }
    assert "note" in body


def test_ttl_disposition_aggregates_by_day(client, app):
    """有数据 → 按天聚合五档，total 累加。"""
    from sgme.engine.recall_aging import (
        ensure_ttl_disposition_table,
        record_disposition,
    )

    conn = app.state.mem_conn
    ensure_ttl_disposition_table(conn)
    record_disposition(conn, "m1", "renewed", None, "2026-12-01T00:00:00Z")
    record_disposition(conn, "m2", "renewed", None, "2026-12-01T00:00:00Z")
    record_disposition(conn, "m3", "archived", None, None)
    conn.commit()

    r = client.get("/v1/admin/ops/ttl-disposition?days=30", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["renewed"] == 2
    assert body["items"][0]["archived"] == 1
    assert body["total"]["renewed"] == 2
    assert body["total"]["archived"] == 1


def test_llm_cost_empty_when_table_missing(client):
    """用量账本未创建 → 200 + 空 items + 零 totals。"""
    r = client.get("/v1/admin/ops/llm-cost", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["totals"] == {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


def test_llm_cost_aggregates(client, app):
    """有数据 → 逐行返回 + totals 累加。"""
    from sgme.data import llm_usage_dao

    conn = app.state.mem_conn
    llm_usage_dao.ensure_table(conn)
    llm_usage_dao.record_usage(
        conn, day="2026-11-01", provider="magpie", model="glm", stage="l1_extraction",
        prompt_tokens=1000, completion_tokens=200,
    )
    llm_usage_dao.record_usage(
        conn, day="2026-11-01", provider="agnes", model="agnes-2.5-flash", stage="l1_conflict",
        prompt_tokens=500, completion_tokens=100,
    )
    conn.commit()

    r = client.get("/v1/admin/ops/llm-cost?days=365", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["totals"]["prompt_tokens"] == 1500
    assert body["totals"]["completion_tokens"] == 300
    assert body["totals"]["calls"] == 2


def test_requires_admin_key(client):
    """无 key → 401/403（鉴权前置）。"""
    r = client.get("/v1/admin/ops/llm-cost")
    assert r.status_code in (401, 403)


# ---------- GET /v1/admin/ops/health-overview（F4 WAL 巡检随该端点返回） ----------

def test_health_overview_exposes_wal_alert(client, app):
    """storage 段必须带 memory_wal_alert / memory_wal_threshold_bytes（WebUI 直接消费）。"""
    r = client.get("/v1/admin/ops/health-overview", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    st = r.json()["storage"]
    assert isinstance(st["memory_wal_bytes"], int)
    assert st["memory_wal_alert"] is False
    assert st["memory_wal_threshold_bytes"] == 200 * 1024 * 1024
    assert st["memory_wal_ratio_threshold"] == 0.5


def test_health_overview_requires_admin_key(client):
    """运维总览是 admin 档端点：agent key 不够。"""
    r = client.get("/v1/admin/ops/health-overview", headers={"X-API-Key": "test-agent-key"})
    assert r.status_code == 403