"""tests/test_memory_usage_dao.py：记忆调用统计数据层（T-251，2026-10-10）。

覆盖：
1. record：同组累加 / 去重 / 多源多调用方分行 / 按天分桶 / 空表不写 / 非法 source
2. record best-effort：表缺失 → 返回 False 不抛（主链路不受影响）
3. query：聚合 + 过滤 + 两种排序 + LEFT JOIN memories 头部字段（含非池中记忆）
4. query_unused：窗口语义（窗口内有调用不出现 / 窗口外旧调用出现 / 非 active 不计）
5. prune：399 / 400 / 401 天边界

零真实网络、零 LLM：纯 SQL + 本地 tmp 库。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, memory_usage_dao


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def _insert_memory(mem_conn, content, dims=None, created_at=None):
    return memory_dao.insert_memory(
        mem_conn,
        content=content,
        memory_type="persona",
        priority=60,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=dims or ["goals"],
        created_at=created_at,
    )


def _rows(mem_conn):
    return [
        dict(r)
        for r in mem_conn.execute(
            "SELECT * FROM memory_usage_daily ORDER BY memory_id, source, caller, day"
        ).fetchall()
    ]


def _day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


# ---------- 1. record ----------


def test_record_accumulates_and_tracks_last_ts(mem_conn):
    memory_usage_dao.record_usage(
        mem_conn, ["m1"], "search", "default", ts="2026-10-09T01:00:00Z"
    )
    assert (
        memory_usage_dao.record_usage(
            mem_conn, ["m1"], "search", "default", ts="2026-10-09T02:00:00Z"
        )
        is True
    )
    rows = _rows(mem_conn)
    assert len(rows) == 1
    assert rows[0]["hits"] == 2
    assert rows[0]["last_ts"] == "2026-10-09T02:00:00Z"  # 最近一次


def test_record_dedupes_ids_within_one_call(mem_conn):
    """同一调用内重复 id 只计一次（去重保序；跨 section 收集依赖此语义）。"""
    memory_usage_dao.record_usage(mem_conn, ["a", "b", "a"], "inject", "default")
    rows = _rows(mem_conn)
    assert {(r["memory_id"], r["hits"]) for r in rows} == {("a", 1), ("b", 1)}


def test_record_splits_by_source_and_caller(mem_conn):
    """行粒度 = (day, memory_id, source, caller)：换来源或换调用方另起一行。"""
    memory_usage_dao.record_usage(mem_conn, ["m1"], "search", "default")
    memory_usage_dao.record_usage(mem_conn, ["m1"], "inject", "default")
    memory_usage_dao.record_usage(mem_conn, ["m1"], "search", "workbuddy")
    rows = _rows(mem_conn)
    assert {(r["source"], r["caller"], r["hits"]) for r in rows} == {
        ("search", "default", 1),
        ("inject", "default", 1),
        ("search", "workbuddy", 1),
    }


def test_record_day_bucketing(mem_conn):
    memory_usage_dao.record_usage(
        mem_conn, ["m1"], "search", "default", ts="2026-10-09T23:00:00Z"
    )
    memory_usage_dao.record_usage(
        mem_conn, ["m1"], "search", "default", ts="2026-10-10T01:00:00Z"
    )
    rows = _rows(mem_conn)
    assert [(r["day"], r["hits"]) for r in rows] == [
        ("2026-10-09", 1),
        ("2026-10-10", 1),
    ]


def test_record_empty_ids_is_noop_true(mem_conn):
    assert memory_usage_dao.record_usage(mem_conn, [], "search", "default") is True
    assert (
        memory_usage_dao.record_usage(mem_conn, ["", ""], "inject", "default") is True
    )
    assert _rows(mem_conn) == []


def test_record_invalid_source_raises(mem_conn):
    with pytest.raises(ValueError, match="未知来源"):
        memory_usage_dao.record_usage(mem_conn, ["m1"], "recall", "default")
    assert _rows(mem_conn) == []


def test_record_best_effort_when_table_missing(mem_conn):
    """表缺失（老库未迁移 / 库结构异常）→ 返回 False 不抛（best-effort 铁律）。"""
    mem_conn.execute("DROP TABLE memory_usage_daily")
    mem_conn.commit()
    assert memory_usage_dao.record_usage(mem_conn, ["m1"], "search", "default") is False


# ---------- 3. query ----------


def test_query_aggregates_filters_and_joins(mem_conn):
    mid1 = _insert_memory(mem_conn, "常被用的记忆内容")
    mid2 = _insert_memory(mem_conn, "偶发使用的记忆内容")
    for _ in range(3):
        memory_usage_dao.record_usage(mem_conn, [mid1], "search", "default")
    memory_usage_dao.record_usage(mem_conn, [mid1], "inject", "dsh")
    memory_usage_dao.record_usage(mem_conn, [mid2], "search", "default")
    # 非池中记忆（已归档 / 脏 id）——调用事实保留，JOIN 字段为 NULL
    memory_usage_dao.record_usage(mem_conn, ["ghost-id"], "search", "default")

    items = memory_usage_dao.query_usage(mem_conn, days=30)
    by = {(i["memory_id"], i["source"]): i for i in items}
    assert by[(mid1, "search")]["hits"] == 3
    assert by[(mid1, "inject")]["hits"] == 1
    assert by[(mid2, "search")]["hits"] == 1
    # LEFT JOIN memories：头部字段 + 类型/状态
    assert by[(mid1, "search")]["content_head"] == "常被用的记忆内容"
    assert by[(mid1, "search")]["memory_type"] == "persona"
    assert by[(mid1, "search")]["status"] == "active"
    # 非池中记忆：JOIN 列为 NULL，行仍在
    assert by[("ghost-id", "search")]["content_head"] is None
    assert by[("ghost-id", "search")]["memory_type"] is None

    # 默认 order=hits：次数降序，首位是 3 次的组合
    assert items[0]["memory_id"] == mid1 and items[0]["hits"] == 3

    # 过滤：source / caller / memory_id
    only_inject = memory_usage_dao.query_usage(mem_conn, days=30, source="inject")
    assert [(i["memory_id"], i["source"]) for i in only_inject] == [(mid1, "inject")]
    only_caller = memory_usage_dao.query_usage(mem_conn, days=30, caller="dsh")
    assert [(i["memory_id"], i["source"]) for i in only_caller] == [(mid1, "inject")]
    only_mid = memory_usage_dao.query_usage(mem_conn, days=30, memory_id=mid2)
    assert [(i["memory_id"], i["hits"]) for i in only_mid] == [(mid2, 1)]

    # limit
    assert len(memory_usage_dao.query_usage(mem_conn, days=30, limit=2)) == 2

    # 窗口过滤：老于窗口的行不出现（另录一条 2020 年的旧行，hits 不变）
    memory_usage_dao.record_usage(
        mem_conn, [mid2], "search", "default", ts="2020-01-01T00:00:00Z"
    )
    assert (
        memory_usage_dao.query_usage(mem_conn, days=30, memory_id=mid2)[0]["hits"] == 1
    )


def test_query_order_last_ts(mem_conn):
    memory_usage_dao.record_usage(
        mem_conn,
        ["m1"],
        "search",
        "default",
        ts=(datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds"),
    )
    memory_usage_dao.record_usage(
        mem_conn,
        ["m2"],
        "search",
        "default",
        ts=(datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds"),
    )
    items = memory_usage_dao.query_usage(mem_conn, days=30, order="last_ts")
    assert [i["memory_id"] for i in items] == ["m2", "m1"]  # 最近调用降序
    # hits 排序相反（各 1 次 → 稳定按 last_ts 降序回退）
    items_hits = memory_usage_dao.query_usage(mem_conn, days=30, order="hits")
    assert [i["memory_id"] for i in items_hits] == ["m2", "m1"]


# ---------- 4. query_unused ----------


def test_query_unused_window_semantics(mem_conn):
    used_recent = _insert_memory(
        mem_conn, "最近被用过的记忆", created_at="2026-10-03T00:00:00Z"
    )
    used_old = _insert_memory(
        mem_conn, "只在窗口外被用过的记忆", created_at="2026-10-02T00:00:00Z"
    )
    never = _insert_memory(
        mem_conn, "从未被用过的记忆", created_at="2026-10-01T00:00:00Z"
    )
    rejected = _insert_memory(
        mem_conn, "被标记不采用的记忆", created_at="2026-10-04T00:00:00Z"
    )
    mem_conn.execute(
        "UPDATE memories SET status='rejected' WHERE memory_id=?", (rejected,)
    )
    mem_conn.commit()

    memory_usage_dao.record_usage(
        mem_conn, [used_recent], "search", "default"
    )  # 窗口内
    memory_usage_dao.record_usage(
        mem_conn, [used_old], "inject", "default", ts="2020-01-01T00:00:00Z"
    )  # 窗口外旧调用

    unused = memory_usage_dao.query_unused(mem_conn, days=30)
    ids = [i["memory_id"] for i in unused]
    assert used_recent not in ids  # 窗口内有过调用 → 不出现
    assert used_old in ids  # 窗口外旧调用 → 本窗口仍算未被使用
    assert never in ids  # 从未调用
    assert rejected not in ids  # 只列 active
    # created_at DESC 排序（显式错开时间，避免同秒并列）
    assert ids == [used_old, never]
    # 字段面
    row = next(i for i in unused if i["memory_id"] == never)
    assert row["content_head"] == "从未被用过的记忆"
    assert row["memory_type"] == "persona"
    assert row["created_at"] == "2026-10-01T00:00:00Z"


def test_query_unused_limits(mem_conn):
    for i in range(3):
        _insert_memory(
            mem_conn, f"未用记忆{i}", created_at=f"2026-10-0{i + 1}T00:00:00Z"
        )
    assert len(memory_usage_dao.query_unused(mem_conn, days=30, limit=2)) == 2


# ---------- 5. prune ----------


def test_prune_boundary_399_400_401(mem_conn):
    """保留期边界：399 天 / 400 天保留，401 天删除（day < cutoff 语义）。"""
    memory_usage_dao.record_usage(
        mem_conn, ["keep-399"], "search", "default", ts=f"{_day(399)}T10:00:00Z"
    )
    memory_usage_dao.record_usage(
        mem_conn, ["keep-400"], "search", "default", ts=f"{_day(400)}T10:00:00Z"
    )
    memory_usage_dao.record_usage(
        mem_conn, ["drop-401"], "search", "default", ts=f"{_day(401)}T10:00:00Z"
    )
    removed = memory_usage_dao.prune_memory_usage(mem_conn, keep_days=400)
    assert removed == 1
    left = {r["memory_id"] for r in _rows(mem_conn)}
    assert left == {"keep-399", "keep-400"}
    # 二次 prune 无行可删
    assert memory_usage_dao.prune_memory_usage(mem_conn, keep_days=400) == 0
