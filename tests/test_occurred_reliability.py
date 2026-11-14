# -*- coding: utf-8 -*-
"""B323 时间锚可信度指标（occurred_equal_ratio）的回归测试（2026-11-13）。"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, stats_dao
from sgme.operations import health as health_ops


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _mem(conn, n, *, equal=True):
    created = "2026-11-13T10:00:00Z"
    occurred = created if equal else "2026-06-17T10:00:00Z"
    memory_dao.insert_memory(
        conn, f"测试记忆 {n}：用户偏好深色主题",
        memory_type="fact", priority=70, time_velocity="static",
        ttl_days=None, dimension_ids=["preferences"],
        created_at=created, occurred_at=occurred,
    )


def test_counts_equal_and_comparable(mem_conn):
    _mem(mem_conn, 1, equal=True)
    _mem(mem_conn, 2, equal=True)
    _mem(mem_conn, 3, equal=False)
    report = stats_dao.occurred_reliability(mem_conn)
    assert report["active_total"] == 3
    assert report["comparable"] == 3
    assert report["equal_to_created"] == 2
    assert report["equal_ratio"] == pytest.approx(0.6667, abs=1e-3)


def test_empty_db_ratio_none(mem_conn):
    report = stats_dao.occurred_reliability(mem_conn)
    assert report["active_total"] == 0
    assert report["equal_ratio"] is None


def test_block_not_degraded_below_sample(mem_conn, cfg):
    _mem(mem_conn, 1, equal=True)
    block = health_ops._occurred_block(mem_conn, cfg)
    assert block["degraded"] is False
    assert block["warn_threshold"] == health_ops.OCCURRED_EQUAL_RATIO_WARN
    n = mem_conn.execute("SELECT COUNT(*) AS c FROM signal_events WHERE type='anomaly_warn'").fetchone()["c"]
    assert n == 0


def test_block_degraded_publishes_signal_once(mem_conn, cfg, monkeypatch):
    monkeypatch.setattr(health_ops, "OCCURRED_MIN_SAMPLE", 1)
    # T4 起告警判定走增量口径，样本线同样要放行小样本
    monkeypatch.setattr(health_ops, "OCCURRED_RECENT_MIN_SAMPLE", 1)
    monkeypatch.setattr(health_ops, "_OCCURRED_ALERT_LAST_PUBLISHED", None)
    _mem(mem_conn, 1, equal=True)
    block = health_ops._occurred_block(mem_conn, cfg)
    assert block["degraded"] is True
    rows = mem_conn.execute(
        "SELECT source, payload FROM signal_events WHERE type='anomaly_warn'"
    ).fetchall()
    assert [r["source"] for r in rows] == ["occurred_at_fallback"]
    assert "occurred_reliability" in rows[0]["payload"]
    # 节流：第二次不再发布
    health_ops._occurred_block(mem_conn, cfg)
    n = mem_conn.execute("SELECT COUNT(*) AS c FROM signal_events").fetchone()["c"]
    assert n == 1


def test_http_payload_carries_block():
    data = {
        "status": "ok", "version": "9.9.9", "llm": {}, "refinement": {}, "vector": {},
        "model_config": {}, "update_available": False, "latest_version": None,
        "update_checked_at": None, "update_error": None, "onboarding": {},
        "credentials_ok": None,
        "occurred_reliability": {"active_total": 1, "equal_ratio": 0.0},
    }
    payload = health_ops.http_payload(data)
    assert payload["occurred_reliability"]["equal_ratio"] == 0.0
