# -*- coding: utf-8 -*-
"""tests/test_cost_alert.py：P1-9 成本告警线回归（设计 §7.2「6. 成本红线与告警」）。

口径：
- 月度 token = 当前自然月 llm_usage_daily 合计；
- 基线默认估算 10M；warn=1.2×、alert=2×；满 3 个完整月可滚动中位数 ×1.1；
- 只告警不停服；同月同档只发一次（signal_events 去重）；
- 运维页 budget_status 只读无副作用。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sgme import config as sgme_config
from sgme.data import db
from sgme.data import llm_usage_dao
from sgme.engine import cost_alert


def _month(offset: int = 0) -> str:
    now = datetime.now(UTC)
    y, m = now.year, now.month
    m2 = m + offset
    while m2 <= 0:
        m2 += 12
        y -= 1
    while m2 > 12:
        m2 -= 12
        y += 1
    return f"{y:04d}-{m2:02d}"


def _add(conn, tokens: int, month: str, day: str = "01"):
    llm_usage_dao.record_usage(
        conn, f"{month}-{day}", "agnes", "agnes-2.5-flash", "l1_extraction",
        prompt_tokens=tokens - 100, completion_tokens=100,
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.connect_memory(tmp_path)
    llm_usage_dao.ensure_table(c)
    yield c
    c.close()


def _cfg(**over):
    base = {"enabled": True, "baseline_tokens": 10_000_000, "auto_baseline": False}
    base.update(over)
    return {"cost_alert": base}


def test_levels_none_warn_alert(conn):
    _add(conn, 10_000_000, _month())
    st = cost_alert.budget_status(conn, _cfg())
    assert st["level"] == "none" and st["ratio"] == 1.0
    conn.execute("DELETE FROM llm_usage_daily")
    _add(conn, 12_500_000, _month())
    assert cost_alert.budget_status(conn, _cfg())["level"] == "warn"
    conn.execute("DELETE FROM llm_usage_daily")
    _add(conn, 20_000_000, _month())
    assert cost_alert.budget_status(conn, _cfg())["level"] == "alert"


def test_auto_baseline_rolling_median(conn):
    for off in (-1, -2, -3):
        _add(conn, 5_000_000, _month(off))
    _add(conn, 6_000_000, _month())
    st = cost_alert.budget_status(conn, _cfg(auto_baseline=True))
    assert st["baseline_source"] == "rolling_median"
    assert st["baseline_tokens"] == int(round(5_000_000 * 1.1))
    assert st["level"] == "none"


def test_alert_publishes_once_per_month_level(conn):
    _add(conn, 3_000_000, _month())
    cfg = _cfg(baseline_tokens=1_000_000)
    first = cost_alert.evaluate_and_alert(conn, cfg)
    assert first["level"] == "alert" and first["alerted"] is True
    second = cost_alert.evaluate_and_alert(conn, cfg)
    assert second["alerted"] is False, "同月同档不得重复告警"
    n = conn.execute(
        "SELECT COUNT(*) c FROM signal_events WHERE source='cost_alert'"
    ).fetchone()["c"]
    assert n == 1


def test_disabled_sends_nothing(conn):
    _add(conn, 9_000_000, _month())
    st = cost_alert.evaluate_and_alert(conn, _cfg(baseline_tokens=1_000_000, enabled=False))
    assert st["level"] == "alert" and st["alerted"] is False
    assert conn.execute(
        "SELECT COUNT(*) c FROM signal_events WHERE source='cost_alert'"
    ).fetchone()["c"] == 0


def test_missing_table_is_graceful(tmp_path):
    c = db.connect_memory(tmp_path)
    c.execute("DROP TABLE IF EXISTS llm_usage_daily")
    c.commit()
    st = cost_alert.budget_status(c, _cfg())
    assert st["available"] is False and st["level"] == "none"
    c.close()


def test_load_config_has_cost_alert_section():
    cfg = sgme_config.load_config()
    assert cfg["cost_alert"]["warn_ratio"] == 1.2
    assert cfg["cost_alert"]["alert_ratio"] == 2.0
    assert cfg["cost_alert"]["baseline_tokens"] == 10_000_000
