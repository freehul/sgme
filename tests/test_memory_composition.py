"""tests/test_memory_composition.py：记忆组成指标（v0.3 S5，2026-11-14）。

验收：
1. 口径：person 覆盖去重计数 / total，ratio 不因多维度超 1
2. route_dist 按 memories.route 分组，NULL 归 "(null)"
3. 空库不炸
4. health 里带 memory_composition 且带告警阈值判定（D2：只告警不阻断）
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao, stats_dao
from sgme.operations import health as health_ops


@pytest.fixture
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    cfg = config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _add(conn, mid, dims=(), route=None, content="内容"):
    now = "2026-11-14T00:00:00Z"
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity,"
        " created_at, updated_at, status, route)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (mid, content, "fact", 50, "slow", now, now, "active", route),
    )
    for d in dims:
        conn.execute(
            "INSERT OR IGNORE INTO memory_tags (memory_id, dimension_id) VALUES (?,?)",
            (mid, d),
        )
    conn.commit()


def test_empty_db(mem_conn):
    comp = stats_dao.memory_composition(mem_conn)
    assert comp["total"] == 0
    assert comp["person_ratio"] == 0.0
    assert comp["route_dist"] == {}


def test_person_and_tech_ratio(mem_conn):
    _add(mem_conn, "m1", ["preferences"])          # person
    _add(mem_conn, "m2", ["habits", "goals"])      # person（多维度只算一次）
    _add(mem_conn, "m3", ["tech_stack"])           # tech
    _add(mem_conn, "m4", ["tech_stack", "preferences"])  # 两边都算（口径不互斥）
    comp = stats_dao.memory_composition(mem_conn)
    assert comp["total"] == 4
    assert comp["person_covered"] == 3
    assert comp["person_ratio"] == 0.75
    assert comp["tech_covered"] == 2
    assert comp["tech_ratio"] == 0.5
    # 多维度不重复计数：person 覆盖数不得超过 total
    assert comp["person_covered"] <= comp["total"]


def test_route_dist_and_len(mem_conn):
    _add(mem_conn, "a", route="memory", content="x" * 10)
    _add(mem_conn, "b", route="skill_candidate", content="y" * 20)
    _add(mem_conn, "c", route=None, content="z" * 30)
    comp = stats_dao.memory_composition(mem_conn)
    assert comp["route_dist"] == {"memory": 1, "skill_candidate": 1, "(null)": 1}
    assert comp["avg_len"] == 20.0
    assert comp["max_len"] == 30


def test_extended_ratio_covers_focus_skills_ideas(mem_conn):
    _add(mem_conn, "m1", ["focus"])
    comp = stats_dao.memory_composition(mem_conn)
    assert comp["person_covered"] == 0            # 严格口径不含 focus
    assert comp["person_covered_extended"] == 1   # 扩展口径含


def test_health_block_reports_and_warns(mem_conn):
    # 全是技术 → person_ratio=0 < 0.5 → 告警
    _add(mem_conn, "t1", ["tech_stack"])
    _add(mem_conn, "t2", ["environment"])
    block = health_ops._composition_block(mem_conn)
    assert block["available"] is True
    assert block["person_ratio"] == 0.0
    assert block["person_ratio_low"] is True
    assert block["person_ratio_warn_threshold"] == 0.5


def test_health_block_never_raises(mem_conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("统计炸了")

    monkeypatch.setattr(stats_dao, "memory_composition", boom)
    block = health_ops._composition_block(mem_conn)
    assert block["available"] is False and "统计炸了" in block["error"]
