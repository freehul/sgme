# -*- coding: utf-8 -*-
"""tests/test_ttl_disposition.py：D-52 TTL 到期五档处置回归。

覆盖（对应设计 §3.3.1 判定顺序）：
- 档 1 续期：M 高档 + 续期未满 → ttl_renewed_at/ttl_renew_count/lifecycle 正确，审计留行；
- 档 2/3 锚点块：续期满 或 中档 → inject_tier=anchor；
- 档 4 休眠：低档 → lifecycle=sleeping + inject_tier=none；
- 闸门 A：planned + valid_to 已过 → 只退出注入（inject_tier=none），不进 1–4 档；
- 0″：planned 未到期 → 即使维度 TTL 到期也不降档；
- 0′：未到期 → 不处置、不写审计行；
- 存量防跑步机：旧到期日早于 1 个 TTL 周期 → 跳续期档，直接按 M 定档；
- 休眠满 30 天 → archived；
- **2.0 不再写 status='expired'**（D-46 三轴分离）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sgme.data import db
from sgme.engine import ttl_disposition as td


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(days_ago: float) -> str:
    return (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


CFG = {
    "ttl": {
        "disposition": {
            "enabled": True,
            "renew_max": 2,
            "tier_high_ratio": 0.67,
            "tier_low_ratio": 0.33,
            "sleep_days": 30,
            "legacy_renew_periods": 1,
        }
    }
}


def _seed_dim(conn, dim_id="identity") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry "
        "(id, display_name, category, time_velocity, created_at) VALUES (?,?,?,?,?)",
        (dim_id, "身份", "静态", "static", _iso(0)),
    )


def _insert(
    conn,
    memory_id: str,
    *,
    ttl_days: float | None = 30,
    updated_days_ago: float = 40,
    anchor: float | None = None,
    renew_count: int = 0,
    ttl_renewed_at: str | None = None,
    modality: str = "done",
    valid_to: str | None = None,
    lifecycle: str | None = None,
    inject_tier: str | None = None,
    memory_type: str = "fact",
) -> None:
    _seed_dim(conn)
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, occurred_at, status, ttl_days, meaning_anchor, "
        "ttl_renew_count, ttl_renewed_at, modality, valid_to, lifecycle, inject_tier) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            memory_id, "内容-" + memory_id, memory_type, 50, "dynamic",
            _iso(updated_days_ago + 1), _iso(updated_days_ago), _iso(updated_days_ago),
            "active", ttl_days, anchor, renew_count, ttl_renewed_at,
            modality, valid_to, lifecycle, inject_tier,
        ),
    )
    conn.execute("INSERT INTO memory_tags (memory_id, dimension_id) VALUES (?,?)",
                 (memory_id, "identity"))


def _fill_quantiles(conn) -> None:
    for i, value in enumerate((0.10, 0.20, 0.40, 0.60, 0.90)):
        _insert(conn, f"q{i}", ttl_days=None, anchor=value)
    conn.commit()


def _row(conn, memory_id: str) -> dict:
    cur = conn.execute(
        "SELECT lifecycle, inject_tier, status, ttl_renew_count, ttl_renewed_at, "
        "meaning_anchor FROM memories WHERE memory_id=?", (memory_id,)
    )
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, cur.fetchone()))


def _audits(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT disposition FROM ttl_disposition ORDER BY id").fetchall()]


def test_tier1_renewal_when_high_and_count_below_max(tmp_path):
    conn = db.connect_memory(tmp_path)
    _fill_quantiles(conn)
    _insert(conn, "m", updated_days_ago=40, anchor=0.95)
    conn.commit()
    stats = td.dispose(conn, CFG)
    row = _row(conn, "m")
    assert stats["renewed"] == 1
    assert row["ttl_renew_count"] == 1
    assert row["ttl_renewed_at"] is not None
    assert row["lifecycle"] == "active"
    assert row["inject_tier"] == "full"
    assert _audits(conn) == ["renewed"]
    conn.close()


def test_tier2_anchor_when_renew_exhausted(tmp_path):
    conn = db.connect_memory(tmp_path)
    _fill_quantiles(conn)
    _insert(conn, "m", updated_days_ago=40, anchor=0.95, renew_count=2)
    conn.commit()
    stats = td.dispose(conn, CFG)
    row = _row(conn, "m")
    assert stats["anchor"] == 1
    assert row["inject_tier"] == "anchor"
    assert row["lifecycle"] == "active"
    assert "anchor" in _audits(conn)
    conn.close()


def test_tier3_anchor_when_mid(tmp_path):
    conn = db.connect_memory(tmp_path)
    _fill_quantiles(conn)
    _insert(conn, "m", updated_days_ago=40, anchor=0.40)
    conn.commit()
    td.dispose(conn, CFG)
    row = _row(conn, "m")
    assert row["inject_tier"] == "anchor"
    assert row["lifecycle"] == "active"
    conn.close()


def test_tier4_sleeping_when_low_and_status_untouched(tmp_path):
    conn = db.connect_memory(tmp_path)
    _fill_quantiles(conn)
    _insert(conn, "m", updated_days_ago=40, anchor=0.10)
    conn.commit()
    stats = td.dispose(conn, CFG)
    row = _row(conn, "m")
    assert stats["sleeping"] == 1
    assert row["lifecycle"] == "sleeping"
    assert row["inject_tier"] == "none"
    assert row["status"] == "active", "2.0 起 TTL 到期不写 status=expired（D-46 三轴分离）"
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE status='expired'"
    ).fetchone()[0] == 0
    assert "sleeping" in _audits(conn)
    conn.close()


def test_gate_a_valid_expired_leaves_lifecycle(tmp_path):
    conn = db.connect_memory(tmp_path)
    _fill_quantiles(conn)
    _insert(conn, "plan", ttl_days=30, updated_days_ago=1, anchor=0.95,
            modality="planned", valid_to=_iso(1))
    conn.commit()
    stats = td.dispose(conn, CFG)
    row = _row(conn, "plan")
    assert stats["valid_expired"] == 1
    assert row["inject_tier"] == "none"
    assert row["lifecycle"] is None, "闸门 A 不动 lifecycle（只退出注入）"
    assert _audits(conn) == ["valid_expired"]
    conn.close()


def test_future_planned_exempt_from_ttl_decay(tmp_path):
    conn = db.connect_memory(tmp_path)
    _fill_quantiles(conn)
    _insert(conn, "future", ttl_days=30, updated_days_ago=200, anchor=0.95,
            modality="planned", valid_to=(_now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    conn.commit()
    stats = td.dispose(conn, CFG)
    assert stats["renewed"] + stats["anchor"] + stats["sleeping"] == 0
    assert _audits(conn) == [], "计划未到期不处置、不记账"
    conn.close()


def test_not_expired_no_disposition(tmp_path):
    conn = db.connect_memory(tmp_path)
    _fill_quantiles(conn)
    _insert(conn, "fresh", ttl_days=30, updated_days_ago=5, anchor=0.10)
    conn.commit()
    td.dispose(conn, CFG)
    assert _audits(conn) == []
    assert _row(conn, "fresh")["lifecycle"] is None
    conn.close()


def test_stale_stock_skips_renewal_treadmill(tmp_path):
    conn = db.connect_memory(tmp_path)
    _fill_quantiles(conn)
    # 旧到期日 = 200 天前 + 30 天 = 170 天前 >> 1 个 TTL 周期 → 防跑步机：不续期
    _insert(conn, "stale", ttl_days=30, updated_days_ago=200, anchor=0.95)
    conn.commit()
    stats = td.dispose(conn, CFG)
    row = _row(conn, "stale")
    assert stats["renewed"] == 0
    assert stats["anchor"] == 1
    assert row["inject_tier"] == "anchor"
    assert row["ttl_renew_count"] == 0
    conn.close()


def test_archive_sleeping_after_sleep_days(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "old_sleep", ttl_days=30, updated_days_ago=90, anchor=0.1,
            lifecycle="sleeping", inject_tier="none")
    conn.commit()
    conn.execute(
        "INSERT INTO ttl_disposition (memory_id, disposition, m_score, renew_count, created_at) "
        "VALUES ('old_sleep','sleeping',0.1,0,?)",
        (_iso(40),),
    )
    conn.commit()
    archived = td.archive_sleeping(conn, 30)
    assert archived == 1
    row = _row(conn, "old_sleep")
    assert row["lifecycle"] == "archived"
    assert row["inject_tier"] == "none"
    assert "archived" in _audits(conn)
    conn.close()


def test_disabled_switch_falls_back_to_legacy(tmp_path):
    """开关关闭时 dream 走 1.x 路径——本模块本身不读 enabled，由 dream 分流；
    这里锁定 cfg 读取口径：unknown 键不炸、缺省值齐全。"""
    conn = db.connect_memory(tmp_path)
    dcfg = td._disposition_cfg({})
    assert dcfg["renew_max"] == 2 and dcfg["sleep_days"] == 30
    assert td._disposition_cfg({"ttl": {"disposition": {"renew_max": 1}}})["renew_max"] == 1
    conn.close()
