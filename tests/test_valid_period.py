# -*- coding: utf-8 -*-
"""T-138 测试：有效期间（valid_from/valid_to）。

覆盖：迁移幂等 / 写入 / 检索过期过滤（NULL 兼容 / 未过期保留 / 开关关闭 /
向量与图路径同样过滤）/ 归档拷贝。
"""

from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db as db_mod, memory_dao
from sgme.data import search as search_mod


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    from sgme import config
    cfg = config.load_config()
    c = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(c, cfg["dimensions"], cfg["aliases"])
    yield c
    db_mod.close(c)


def _ins(conn, content, dim_ids=None, **kw):
    return memory_dao.insert_memory(
        conn, content=content, memory_type=kw.get("memory_type", "persona"),
        priority=kw.get("priority", 60), time_velocity=kw.get("time_velocity", "static"),
        ttl_days=kw.get("ttl_days"), dimension_ids=dim_ids or ["goals"],
        valid_from=kw.get("valid_from"), valid_to=kw.get("valid_to"),
    )


# ---------- 迁移 ----------

def test_migrate_valid_period_idempotent(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    db_mod._migrate_mem_valid_period(conn)
    db_mod._migrate_mem_valid_period(conn)   # 第二次无副作用
    for table in ("memories", "memory_archive"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        assert "valid_from" in cols and "valid_to" in cols
    conn.close()


# ---------- 检索过滤 ----------

def test_search_excludes_expired(conn):
    _ins(conn, "alpha 项目已结束", valid_to="2026-01-01T00:00:00Z")   # 已过期
    _ins(conn, "beta 项目进行中", valid_to="2099-12-31T00:00:00Z")    # 未过期
    _ins(conn, "gamma 永久事实")                                       # NULL 永久有效
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False,
        cfg={"search": {"vector": {"enabled": False}}},
    )
    assert res == []          # 唯一命中已过期 → 过滤空


def test_search_keeps_active_and_null(conn):
    _ins(conn, "alpha 项目已结束", valid_to="2026-01-01T00:00:00Z")
    _ins(conn, "alpha beta 项目进行中", valid_to="2099-12-31T00:00:00Z")
    _ins(conn, "alpha gamma 永久事实")
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False,
        cfg={"search": {"vector": {"enabled": False}}},
    )
    ids = [r["memory_id"] for r in res]
    assert len(ids) == 2      # 未过期 + NULL 保留，过期剔除
    # 未过期与永久有效都在
    assert all(r["content"] != "alpha 项目已结束" for r in res)


def test_search_valid_period_disabled(conn):
    """开关关闭 → 过期也召回（灰度/回归对照）。"""
    _ins(conn, "alpha 项目已结束", valid_to="2026-01-01T00:00:00Z")
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False,
        cfg={"search": {"vector": {"enabled": False}, "valid_period": {"enabled": False}}},
    )
    assert len(res) == 1      # 开关关 → 不过滤


def test_filter_expired_unit(conn):
    """_filter_expired 单测：批量过滤 + 空输入。"""
    _ins(conn, "alpha 旧事实", valid_to="2026-01-01T00:00:00Z")
    mid = _ins(conn, "alpha 新事实")
    results = [{"memory_id": mid}]
    assert search_mod._filter_expired(conn, results) == results      # 无过期 → 原样
    assert search_mod._filter_expired(conn, []) == []                # 空 → 空


def test_search_graph_and_vector_paths_also_filtered(conn):
    """向量/图路径同样过滤（_filter_expired 在 RRF 融合后统一执行）。"""
    _ins(conn, "alpha 向量事实", valid_to="2026-01-01T00:00:00Z")
    # 向量不可用（临时库无向量）时走 BM25；配置 graph 开（无边表 → 空）
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False,
        cfg={"search": {"vector": {"enabled": True}, "graph": {"enabled": True}}},
    )
    assert res == []          # 唯一候选过期 → 过滤（无论向量/图是否贡献）


# ---------- 归档拷贝 ----------

def test_archive_copies_valid_columns(conn):
    mid = _ins(conn, "alpha 待归档", valid_from="2026-01-01T00:00:00Z",
               valid_to="2026-06-01T00:00:00Z")
    assert memory_dao.archive_memory(conn, mid, superseded_by="new1")
    row = conn.execute("SELECT * FROM memory_archive WHERE memory_id=?", (mid,)).fetchone()
    assert row["valid_from"] == "2026-01-01T00:00:00Z"
    assert row["valid_to"] == "2026-06-01T00:00:00Z"


def test_insert_defaults_null(conn):
    mid = _ins(conn, "alpha 默认无期间")
    row = conn.execute("SELECT valid_from, valid_to FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row["valid_from"] is None and row["valid_to"] is None
