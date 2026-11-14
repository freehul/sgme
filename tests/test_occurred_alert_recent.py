# -*- coding: utf-8 -*-
"""T4（三缺口补齐 2026-11-14）：时间锚告警增量/存量分离判定。

背景：生产每 1 小时推一条 anomaly_warn（夜里累积 12 条），根因是 40,842 条存量
记忆 occurred_at == created_at 比例超阈值——B323 只修了新提炼，存量是历史问题。
T4 起告警只看增量（最近 7 天 created_at），存量只进 health 展示（stock_degraded）。

验收（依据 docs/plans/任务清单-三缺口补齐-20261114.md T4）：
1. 只有存量超标 → 不发告警（stock_degraded=True、degraded=False）
2. 增量超标 → 发告警（payload 带 scope=recent 与两个 ratio）
3. health 块里两个 ratio 都在
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao
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


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _mem(conn, n, *, created_at, equal=True):
    occurred = created_at if equal else "2026-06-01T08:00:00Z"
    memory_dao.insert_memory(
        conn, f"测试记忆 {n}：用户偏好深色主题",
        memory_type="fact", priority=70, time_velocity="static",
        ttl_days=None, dimension_ids=["preferences"],
        created_at=created_at, occurred_at=occurred,
    )


def _alert_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM signal_events WHERE type='anomaly_warn'"
    ).fetchone()["c"]


def test_stock_only_over_threshold_no_alert(mem_conn, cfg, monkeypatch):
    """存量（窗口外）全 equal、增量无样本 → stock_degraded=True 但不告警。"""
    monkeypatch.setattr(health_ops, "OCCURRED_MIN_SAMPLE", 1)
    monkeypatch.setattr(health_ops, "OCCURRED_RECENT_MIN_SAMPLE", 1)
    monkeypatch.setattr(health_ops, "_OCCURRED_ALERT_LAST_PUBLISHED", None)
    for i in range(3):
        _mem(mem_conn, i, created_at=_iso(8), equal=True)  # 8 天前：窗口外
    block = health_ops._occurred_block(mem_conn, cfg)
    assert block["stock_degraded"] is True
    assert block["degraded"] is False  # 增量口径无样本 → 不告警
    assert _alert_count(mem_conn) == 0


def test_recent_over_threshold_alerts(mem_conn, cfg, monkeypatch):
    """增量（窗口内）超标 → 发告警，payload 带 scope=recent 与两口径 ratio。"""
    monkeypatch.setattr(health_ops, "OCCURRED_RECENT_MIN_SAMPLE", 1)
    monkeypatch.setattr(health_ops, "_OCCURRED_ALERT_LAST_PUBLISHED", None)
    for i in range(2):
        _mem(mem_conn, i, created_at=_iso(1), equal=True)  # 昨天：窗口内
    block = health_ops._occurred_block(mem_conn, cfg)
    assert block["degraded"] is True
    assert block["recent_ratio"] == 1.0
    rows = mem_conn.execute(
        "SELECT source, payload FROM signal_events WHERE type='anomaly_warn'"
    ).fetchall()
    assert [r["source"] for r in rows] == ["occurred_at_fallback"]
    payload = json.loads(rows[0]["payload"])  # signal_events.payload 存 JSON 文本
    assert payload["scope"] == "recent"
    assert payload["recent_ratio"] == 1.0
    assert "equal_ratio" in payload  # 存量口径随行携带，便于对照


def test_health_block_carries_both_ratios(mem_conn, cfg):
    """health 块里存量 equal_ratio 与增量 recent_ratio 同时可见。"""
    _mem(mem_conn, 1, created_at=_iso(8), equal=True)   # 存量 equal（窗口外）
    _mem(mem_conn, 2, created_at=_iso(1), equal=False)  # 增量正常（窗口内）
    block = health_ops._occurred_block(mem_conn, cfg)
    # 存量 = 全量 2 条（1 equal / 2），增量 = 窗口内 1 条（不 equal）
    assert block["equal_ratio"] == pytest.approx(0.5)
    assert block["recent_ratio"] == pytest.approx(0.0)
    assert block["recent_days"] == 7
    assert block["degraded"] is False and block["stock_degraded"] is False


def test_mixed_recent_overrides_stock_quiet(mem_conn, cfg, monkeypatch):
    """存量超标 + 增量正常 → 不告警（T4 核心降噪语义）。"""
    monkeypatch.setattr(health_ops, "OCCURRED_MIN_SAMPLE", 1)
    monkeypatch.setattr(health_ops, "OCCURRED_RECENT_MIN_SAMPLE", 1)
    monkeypatch.setattr(health_ops, "_OCCURRED_ALERT_LAST_PUBLISHED", None)
    for i in range(5):
        _mem(mem_conn, i, created_at=_iso(30), equal=True)   # 存量全 equal
    _mem(mem_conn, 99, created_at=_iso(1), equal=False)      # 增量正常
    block = health_ops._occurred_block(mem_conn, cfg)
    assert block["stock_degraded"] is True
    assert block["degraded"] is False
    assert _alert_count(mem_conn) == 0
