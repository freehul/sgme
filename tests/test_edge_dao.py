# -*- coding: utf-8 -*-
"""edge_dao / memory_edges 测试（ST-38 T-133）。

覆盖：建表幂等、create_edge 幂等、neighbors 双向去重、delete_by_source、
backfill（supersession 双向边 / 场景共现 belongs_to / 每记忆 top_n 截断 /
全局 cap 超限记 anomaly_warn / 幂等重跑 / dry-run 不写库 / 其他 source 保留 /
仅 active 场景参与）。
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db, edge_dao
from sgme.data.edge_dao import SYSTEM_SOURCE


# ---------- 测试库构造 ----------

def _conn(tmp_path) -> sqlite3.Connection:
    return db.connect_memory(tmp_path)


def _mem(conn, mid: str, priority: int = 50, status: str = "active") -> None:
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "ttl_days, created_at, updated_at, status, occurred_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mid, f"content-{mid}", "episodic", priority, "dynamic", None,
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", status, "2026-01-01T00:00:00Z"),
    )


def _scene(conn, sid: str, status: str = "active") -> None:
    conn.execute(
        "INSERT INTO scenes (scene_id, title, content, heat, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (sid, f"scene-{sid}", "c", 1, status, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )


def _scene_mem(conn, sid: str, mid: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO scene_memories (scene_id, memory_id) VALUES (?,?)",
        (sid, mid),
    )


def _archive(conn, mid: str, superseded_by: str | None) -> None:
    conn.execute(
        "INSERT INTO memory_archive (memory_id, content, memory_type, priority, "
        "time_velocity, ttl_days, created_at, updated_at, archived_at, superseded_by, "
        "occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mid, f"arc-{mid}", "episodic", 50, "dynamic", None,
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
         "2026-01-02T00:00:00Z", superseded_by, "2026-01-01T00:00:00Z"),
    )


# ---------- 基础读写 ----------

def test_memory_edges_table_created(tmp_path):
    conn = _conn(tmp_path)
    tables = db.list_tables(conn)
    assert "memory_edges" in tables
    idxs = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memory_edges'")]
    assert "idx_edges_from" in idxs and "idx_edges_to" in idxs
    # 幂等：重复 connect 不报错
    db.close(conn)
    conn2 = _conn(tmp_path)
    assert "memory_edges" in db.list_tables(conn2)
    db.close(conn2)


def test_create_edge_idempotent(tmp_path):
    conn = _conn(tmp_path)
    eid = edge_dao.create_edge(conn, "A", "B", "supersedes", source="system")
    edge_dao.create_edge(conn, "A", "B", "supersedes", source="system")
    conn.commit()
    assert edge_dao.count_edges(conn) == 1
    assert eid == "A::B::supersedes"
    db.close(conn)


def test_delete_edges_by_source(tmp_path):
    conn = _conn(tmp_path)
    edge_dao.create_edge(conn, "A", "B", "supersedes", source="system")
    edge_dao.create_edge(conn, "A", "B", "similar", source="llm")
    conn.commit()
    assert edge_dao.delete_edges_by_source(conn, SYSTEM_SOURCE) == 1
    conn.commit()
    assert edge_dao.count_edges(conn) == 1
    assert edge_dao.count_edges(conn, source="llm") == 1
    db.close(conn)


def test_neighbors_both_directions_and_dedup(tmp_path):
    conn = _conn(tmp_path)
    edge_dao.create_edge(conn, "A", "B", "supersedes", weight=1.0, source="system")
    edge_dao.create_edge(conn, "C", "A", "belongs_to", weight=2.0, source="system")
    edge_dao.create_edge(conn, "A", "B", "similar", weight=3.0, source="llm")
    conn.commit()
    nbrs = edge_dao.neighbors(conn, "A")
    by_id = {n["memory_id"]: n for n in nbrs}
    assert set(by_id) == {"B", "C"}
    # B 出现多关系 → 取最高 weight（similar 3.0）
    assert by_id["B"]["weight"] == 3.0
    assert by_id["C"]["weight"] == 2.0
    # relation 过滤
    only_ss = edge_dao.neighbors(conn, "A", relation="supersedes")
    assert [n["memory_id"] for n in only_ss] == ["B"]
    db.close(conn)


# ---------- backfill：supersession ----------

def test_backfill_supersession_directions(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "B")          # active 新记忆
    _archive(conn, "A", superseded_by="B")  # A 被 B 取代
    conn.commit()
    stats = edge_dao.backfill_system_edges(conn)
    assert stats["superseded_pairs"] == 1
    assert stats["supersedes_edges"] == 1
    assert stats["evolves_from_edges"] == 1
    assert stats["total"] == 2
    rels = {r["relation"] for r in edge_dao.list_edges(conn, source="system")}
    assert rels == {"supersedes", "evolves_from"}
    # supersedes: B -> A；evolves_from: A -> B
    by_rel = {r["relation"]: (r["from_id"], r["to_id"])
              for r in edge_dao.list_edges(conn, source="system")}
    assert by_rel["supersedes"] == ("B", "A")
    assert by_rel["evolves_from"] == ("A", "B")
    # 空 superseded_by 不建边
    _archive(conn, "X", superseded_by=None)
    conn.commit()
    stats2 = edge_dao.backfill_system_edges(conn)
    assert stats2["superseded_pairs"] == 1
    db.close(conn)


# ---------- backfill：场景共现 ----------

def test_backfill_scene_cooccurrence_weight(tmp_path):
    conn = _conn(tmp_path)
    for m in ("m1", "m2", "m3"):
        _mem(conn, m)
    _scene(conn, "s1")
    _scene(conn, "s2")
    _scene_mem(conn, "s1", "m1")
    _scene_mem(conn, "s1", "m2")
    _scene_mem(conn, "s2", "m1")
    _scene_mem(conn, "s2", "m2")
    _scene_mem(conn, "s2", "m3")
    conn.commit()
    stats = edge_dao.backfill_system_edges(conn, top_n=8, min_weight=1)
    # m1-m2 共现 2 个场景 → weight 2
    rows = edge_dao.list_edges(conn, source="system", relation="belongs_to")
    by_pair = {(r["from_id"], r["to_id"]): r["weight"] for r in rows}
    assert by_pair.get(("m1", "m2")) == 2.0
    # m1-m3 只共现 s2 → weight 1
    assert by_pair.get(("m1", "m3")) == 1.0
    assert stats["scene_pairs_raw"] == 4  # s1:1 + s2:3
    db.close(conn)


def test_backfill_scene_topn_truncation(tmp_path):
    conn = _conn(tmp_path)
    mems = [f"m{i}" for i in range(30)]
    for m in mems:
        _mem(conn, m, priority=100 - int(m[1:]))
    _scene(conn, "big")
    for m in mems:
        _scene_mem(conn, "big", m)
    conn.commit()
    stats = edge_dao.backfill_system_edges(conn, top_n=3, per_scene_top_n=100, min_weight=1)
    # 原始组合对 = C(30,2) = 435；top_n=3 截断后总边量 ≤ 30*3 = 90（防爆炸）
    assert stats["scene_pairs_raw"] == 435
    assert 0 < stats["belongs_to_edges"] <= len(mems) * 3
    # 每记忆自己的出度（自己选出的邻居）≤ 3：m0 的 top-3 邻居都有边
    for m in mems:
        # belongs_to 边仅存规范方向；从 m 出发的边（from_id=m 或 to_id=m 且 m 为选中方）
        # 直接断言：m 至少有一条 belongs_to 边（30 个节点不会全孤立），且总量受控
        assert edge_dao.neighbors(conn, m, relation="belongs_to"), f"{m} 无邻居"
    db.close(conn)


def test_backfill_active_scenes_only_and_status_filter(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "m1")
    _mem(conn, "m2", status="rejected")  # 非 active 记忆不参与
    _scene(conn, "sa", status="active")
    _scene(conn, "sb", status="archived")  # 归档场景不参与
    _scene_mem(conn, "sa", "m1")
    _scene_mem(conn, "sb", "m1")
    _scene_mem(conn, "sb", "m2")
    conn.commit()
    stats = edge_dao.backfill_system_edges(conn, top_n=8, min_weight=1)
    assert stats["belongs_to_edges"] == 0  # sa 只有 m1 一条 → 无对
    db.close(conn)


# ---------- backfill：全局上限 + anomaly + 幂等 ----------

def test_backfill_global_cap_anomaly(tmp_path):
    conn = _conn(tmp_path)
    mems = [f"m{i}" for i in range(20)]
    for m in mems:
        _mem(conn, m)
    _scene(conn, "s")
    for m in mems:
        _scene_mem(conn, "s", m)
    conn.commit()
    stats = edge_dao.backfill_system_edges(conn, top_n=8, per_scene_top_n=100,
                                           min_weight=1, global_cap=5)
    assert stats["truncated"] > 0
    assert stats["anomaly"] is not None
    assert stats["total"] <= 5
    # anomaly_warn 已发布到 signal_events
    evs = conn.execute(
        "SELECT * FROM signal_events WHERE type='anomaly_warn' AND source='edge_backfill'"
    ).fetchall()
    assert len(evs) >= 1
    assert "edge_total" in evs[0]["payload"]
    db.close(conn)


def test_backfill_idempotent(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "B")
    _archive(conn, "A", superseded_by="B")
    for m in ("m1", "m2", "m3"):
        _mem(conn, m)
    _scene(conn, "s1")
    for m in ("m1", "m2", "m3"):
        _scene_mem(conn, "s1", m)
    conn.commit()
    s1 = edge_dao.backfill_system_edges(conn, top_n=8, min_weight=1)
    c1 = edge_dao.edge_stats(conn)
    s2 = edge_dao.backfill_system_edges(conn, top_n=8, min_weight=1)
    c2 = edge_dao.edge_stats(conn)
    assert s1 == s2
    assert c1 == c2
    db.close(conn)


def test_backfill_dry_run_no_write(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "B")
    _archive(conn, "A", superseded_by="B")
    _scene(conn, "s")
    _scene_mem(conn, "s", "B")
    conn.commit()
    edge_dao.create_edge(conn, "X", "Y", "similar", source="llm")
    conn.commit()
    stats = edge_dao.backfill_system_edges(conn, dry_run=True)
    assert stats["dry_run"] is True
    assert stats["superseded_pairs"] == 1
    # 未写任何 system 边，且既有 llm 边保留
    assert edge_dao.count_edges(conn, source=SYSTEM_SOURCE) == 0
    assert edge_dao.count_edges(conn, source="llm") == 1
    db.close(conn)


def test_backfill_preserves_other_sources(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "B")
    _archive(conn, "A", superseded_by="B")
    conn.commit()
    edge_dao.create_edge(conn, "P", "Q", "similar", weight=0.8, source="llm")
    conn.commit()
    edge_dao.backfill_system_edges(conn)
    assert edge_dao.count_edges(conn, source="llm") == 1
    assert edge_dao.count_edges(conn, source=SYSTEM_SOURCE) == 2
    db.close(conn)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
