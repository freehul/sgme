# -*- coding: utf-8 -*-
"""tests/test_meaning_anchor.py：D-50 意义锚 M（v2 方向修正版）单元与回填回归。

覆盖：
- 内容信号：长期/规则词 > 中性 > 一次性任务词；
- 主料方向：同输入下 identity（1.00）必须高于 tech_stack（0.35）——这是 9.11.5
  「方向性错误」的回归锁，防止未来调权重又把技术栈调回去；
- 边界：M ∈ [0,1]，加分项封顶 0.10；
- 回填：只写 active 行、不动 rejected；distribution / tier_thresholds 用分位。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sgme.data import db
from sgme.engine import meaning_anchor as ma


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(days_ago: float) -> str:
    return (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_dim(conn, dim_id: str, name: str = "身份") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry "
        "(id, display_name, category, time_velocity, created_at) VALUES (?,?,?,?,?)",
        (dim_id, name, "静态", "static", _iso(0)),
    )


def _insert(
    conn,
    memory_id: str,
    *,
    content: str = "用户是独立开发者，长期用 Python。",
    occurred_days_ago: float = 30,
    dim: str = "identity",
    status: str = "active",
    feeling: str | None = None,
) -> None:
    _seed_dim(conn, dim)
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, occurred_at, status, feeling) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (memory_id, content, "fact", 50, "static", _iso(occurred_days_ago),
         _iso(0), _iso(occurred_days_ago), status, feeling),
    )
    conn.execute(
        "INSERT INTO memory_tags (memory_id, dimension_id) VALUES (?,?)", (memory_id, dim)
    )


# ---------- 内容信号 ----------


def test_content_signal_orders_long_term_over_one_off():
    neutral = ma.content_signal("周五下午开会")
    long_term = ma.content_signal("用户一直坚持用简体中文，固定 14px 字号")
    one_off = ma.content_signal("这次临时试一下，帮我改一下")
    assert one_off < neutral < long_term
    assert 0.0 <= one_off <= 1.0 and 0.0 <= long_term <= 1.0


# ---------- 主料方向（9.11.5 回归锁） ----------


def test_identity_prior_beats_tech_stack():
    common = dict(
        content="用户是独立开发者，长期用 Python。",
        occurred_at=_iso(100),
        updated_at=_iso(0),
        in_degree=2,
        max_in_degree=10,
    )
    m_identity = ma.compute_m(dimension_prior=ma.DIMENSION_PRIOR["identity"], **common)
    m_tech = ma.compute_m(dimension_prior=ma.DIMENSION_PRIOR["tech_stack"], **common)
    assert m_identity > m_tech
    # 方向修正的量化证据：tech_stack 必须明显低于 environment/preferences
    assert ma.DIMENSION_PRIOR["tech_stack"] < ma.DIMENSION_PRIOR["environment"]
    assert ma.DIMENSION_PRIOR["tech_stack"] < ma.DIMENSION_PRIOR["preferences"]


def test_compute_m_bounded_and_bonus_capped():
    m = ma.compute_m(
        content="一直 总是 每次 固定 必须 禁止 原则 底线 " * 5,
        dimension_prior=1.0,
        occurred_at=_iso(3000),
        updated_at=_iso(0),
        in_degree=999,
        max_in_degree=999,
        has_feeling=True,
        recall_count=50,
    )
    assert 0.0 <= m <= 1.0
    base = ma.compute_m(
        content="一直 总是 每次 固定 必须 禁止 原则 底线 " * 5,
        dimension_prior=1.0, occurred_at=_iso(3000), updated_at=_iso(0),
        in_degree=999, max_in_degree=999,
    )
    assert m - base <= 0.10 + 1e-9, "两个加分项合计不得超过 0.10"


# ---------- 回填与分布 ----------


def test_recompute_writes_active_only_and_reports_distribution(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "m1", dim="identity")
    _insert(conn, "m2", dim="tech_stack", content="这次临时跑一下 debug 脚本")
    _insert(conn, "m3", dim="family", status="rejected")
    _insert(conn, "m4", dim="preferences")
    _insert(conn, "m5", dim="goals")
    conn.commit()

    stats = ma.recompute(conn)
    assert stats["count"] == 4, "只回填 active"
    rows = dict(
        conn.execute("SELECT memory_id, meaning_anchor FROM memories").fetchall()
    )
    assert rows["m1"] is not None and rows["m2"] is not None
    assert rows["m3"] is None, "rejected 不写 M（质量轴与意义轴互不污染）"
    assert rows["m1"] > rows["m2"], "长期身份记忆应高于技术一次性调试"

    dist = ma.distribution(conn)
    assert dist["n"] == 4 and 0.0 <= dist["p50"] <= 1.0

    hi, lo = ma.tier_thresholds(conn)
    assert hi is not None and lo is not None
    assert lo <= hi
    conn.close()


def test_tier_thresholds_use_quantiles(tmp_path):
    conn = db.connect_memory(tmp_path)
    for i, value in enumerate((0.10, 0.20, 0.40, 0.60, 0.90)):
        _insert(conn, f"q{i}")
    conn.commit()
    conn.executemany(
        "UPDATE memories SET meaning_anchor=? WHERE memory_id=?",
        [(0.10, "q0"), (0.20, "q1"), (0.40, "q2"), (0.60, "q3"), (0.90, "q4")],
    )
    conn.commit()
    hi, lo = ma.tier_thresholds(conn, high_ratio=0.67, low_ratio=0.33)
    # 线性插值分位：p33 = 0.2 + 0.32*(0.4-0.2) = 0.264；p67 = 0.4 + 0.68*(0.6-0.4) = 0.536
    assert 0.24 <= lo <= 0.30
    assert 0.50 <= hi <= 0.58
    conn.close()


def test_recompute_only_null_skips_existing(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "a")
    _insert(conn, "b")
    conn.commit()
    conn.execute("UPDATE memories SET meaning_anchor=0.123 WHERE memory_id='a'")
    conn.commit()
    stats = ma.recompute(conn, only_null=True)
    assert stats["count"] == 1
    assert conn.execute(
        "SELECT meaning_anchor FROM memories WHERE memory_id='a'"
    ).fetchone()[0] == 0.123
    conn.close()
