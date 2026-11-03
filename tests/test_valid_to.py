# -*- coding: utf-8 -*-
"""D-43 闸门 A 的输入：`valid_to` 写入点（2026-11-03 补齐）。

背景：`valid_to` 列 1.x 就有（ST-39/T-138），但**全仓没有写入点**——闸门 A 永远
无输入，「明年三月体检」这类计划记忆只能按维度 TTL 到期，而不是活到计划日。

口径（D-43）：
- planned → 取计划时间点（只认**未来**的 occurred_at，不编造）
- wished  → now + 90 天窗口
- 其余    → None（已发生/引用类无断言有效期）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sgme.engine.l2_extract import WISHED_WINDOW_DAYS, _valid_to_for


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_wished_gets_ninety_day_window():
    v = _valid_to_for("wished", None)
    assert v is not None
    target = datetime.now(timezone.utc) + timedelta(days=WISHED_WINDOW_DAYS)
    got = datetime.fromisoformat(v.replace("Z", "+00:00"))
    # 允许几秒误差
    assert abs((got - target).total_seconds()) < 300


def test_planned_future_date_kept():
    future = _iso(datetime.now(timezone.utc) + timedelta(days=120))
    assert _valid_to_for("planned", future) == future


def test_planned_past_date_ignored():
    past = _iso(datetime.now(timezone.utc) - timedelta(days=3))
    assert _valid_to_for("planned", past) is None


def test_planned_without_date_ignored():
    assert _valid_to_for("planned", None) is None
    assert _valid_to_for("planned", "") is None
    assert _valid_to_for("planned", "不是日期") is None


def test_other_modalities_have_no_validity():
    for m in ("done", "quoted", "unknown", None, ""):
        assert _valid_to_for(m, _iso(datetime.now(timezone.utc) + timedelta(days=30))) is None


def test_valid_to_persisted(tmp_path):
    """落库链路：insert_memory 收 valid_to 并写进列。"""
    from sgme import config as sgme_config
    from sgme.data import db as db_mod
    from sgme.data import memory_dao

    cfg = sgme_config.load_config()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    future = _iso(datetime.now(timezone.utc) + timedelta(days=120))
    mid = memory_dao.insert_memory(
        mem,
        content="用户计划明年三月去体检",
        memory_type="fact",
        priority=50,
        time_velocity="dynamic",
        ttl_days=90,
        dimension_ids=["goals"],
        occurred_at=future,
        prompt_version="test",
        valid_to=future,
        extra={"type": "想法", "subject": "self", "modality": "planned"},
    )
    row = mem.execute(
        "SELECT valid_to, modality FROM memories WHERE memory_id=?", (mid,)
    ).fetchone()
    assert row["valid_to"] == future
    assert row["modality"] == "planned"
    mem.close()
