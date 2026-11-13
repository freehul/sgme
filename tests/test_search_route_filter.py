"""tests/test_search_route_filter.py：检索侧三平面过滤（v0.3 S3，2026-11-14）。

验收：
1. `search.routing.enabled` 灰度默认关（开之前存量候选会在检索里失联）
2. 关：行为与 1.x 一致（skill_candidate 记忆照常召回）
3. 开：只保留 route IS NULL OR route='memory'；NULL 按 memory 对待
4. 配置合并 + 可写白名单

设计依据：docs/plans/设计-v0.3-三平面分流定稿与实施契约.md §6
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data import search as search_mod


@pytest.fixture
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    cfg = config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _add(conn, memory_id, route):
    now = "2026-11-14T00:00:00Z"
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity,"
        " created_at, updated_at, status, route)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (memory_id, f"内容-{memory_id}", "fact", 50, "slow", now, now, "active", route),
    )
    conn.commit()


def _cfg(enabled: bool) -> dict:
    return {"search": {"routing": {"enabled": enabled}}}


# ---------------- 配置 ----------------

def test_default_off():
    assert config.DEFAULT_SEARCH_CONFIG["routing"]["enabled"] is False


def test_merge_picks_up_routing_flag():
    merged = config._merge_search_config({"routing": {"enabled": True}})
    assert merged["routing"]["enabled"] is True
    assert config._merge_search_config({})["routing"]["enabled"] is False


def test_section_keys_allow_routing():
    assert "routing" in config.SECTION_KEYS["search"]


def test_flag_reader():
    assert search_mod._route_filter_enabled(None) is False
    assert search_mod._route_filter_enabled({}) is False
    assert search_mod._route_filter_enabled(_cfg(False)) is False
    assert search_mod._route_filter_enabled(_cfg(True)) is True


# ---------------- 过滤语义 ----------------

def test_filter_keeps_memory_and_null(mem_conn):
    _add(mem_conn, "m-mem", "memory")
    _add(mem_conn, "m-null", None)
    _add(mem_conn, "m-skill", "skill_candidate")
    _add(mem_conn, "m-wiki", "wiki_candidate")
    rows = [{"memory_id": i} for i in ("m-mem", "m-null", "m-skill", "m-wiki")]

    out = search_mod._filter_by_route(mem_conn, rows, _cfg(True))
    assert [r["memory_id"] for r in out] == ["m-mem", "m-null"]


def test_filter_empty_is_noop(mem_conn):
    assert search_mod._filter_by_route(mem_conn, [], _cfg(True)) == []


def test_filter_drops_unknown_ids(mem_conn):
    """记忆已被删除的 id 一并剔除（正常不会出现，防御性行为）。"""
    _add(mem_conn, "m-ok", "memory")
    rows = [{"memory_id": "m-ok"}, {"memory_id": "m-gone"}]
    out = search_mod._filter_by_route(mem_conn, rows, _cfg(True))
    assert [r["memory_id"] for r in out] == ["m-ok"]


def test_filter_preserves_order(mem_conn):
    for i in range(3):
        _add(mem_conn, f"m-{i}", "memory")
    rows = [{"memory_id": "m-2"}, {"memory_id": "m-0"}, {"memory_id": "m-1"}]
    out = search_mod._filter_by_route(mem_conn, rows, _cfg(True))
    assert [r["memory_id"] for r in out] == ["m-2", "m-0", "m-1"]
