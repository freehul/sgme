# -*- coding: utf-8 -*-
"""edge_dao / memory_edges 测试（ST-38 T-133；T-427 场景层退役后）。

覆盖：建表幂等、create_edge 幂等、neighbors 双向去重、delete_by_source、
backfill（supersession 双向边 / 幂等重跑 / dry-run 不写库 / 其他 source 保留 /
历史场景共现 belongs_to 边保留不删）。
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


# ---------- backfill：历史场景边保留 ----------

def test_backfill_preserves_legacy_scene_edges(tmp_path):
    """T-427：场景共现源摘除后，历史 belongs_to 边保留不删（只重算 supersession）。"""
    conn = _conn(tmp_path)
    _mem(conn, "B")
    _archive(conn, "A", superseded_by="B")
    conn.commit()
    edge_dao.create_edge(
        conn, "m1", "m2", "belongs_to", weight=2.0, source=SYSTEM_SOURCE
    )
    conn.commit()

    stats = edge_dao.backfill_system_edges(conn)
    assert stats["total"] == 2  # 仅 supersedes + evolves_from
    rows = edge_dao.list_edges(conn, source=SYSTEM_SOURCE, relation="belongs_to")
    assert len(rows) == 1
    assert (rows[0]["from_id"], rows[0]["to_id"]) == ("m1", "m2")
    db.close(conn)


# ---------- backfill：幂等 + dry-run + 其他 source 保留 ----------

def test_backfill_idempotent(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "B")
    _archive(conn, "A", superseded_by="B")
    conn.commit()
    s1 = edge_dao.backfill_system_edges(conn)
    c1 = edge_dao.edge_stats(conn)
    s2 = edge_dao.backfill_system_edges(conn)
    c2 = edge_dao.edge_stats(conn)
    assert s1 == s2
    assert c1 == c2
    db.close(conn)


def test_backfill_dry_run_no_write(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "B")
    _archive(conn, "A", superseded_by="B")
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
