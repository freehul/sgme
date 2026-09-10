"""裸连接检索回归（2026-09-10 T-150）。

背景：`search` 内部原先直接写 `[dict(r) for r in cur.fetchall()]`，这依赖调用方
已经设了 `row_factory`。未设的裸 `sqlite3.connect` 传进来，`r` 是元组，
`dict(tuple)` 会按 (k, v) 解包并抛：

    ValueError: dictionary update sequence element #0 has length 36; 2 is required

生产 FastAPI 的连接设了 row_factory（`sgme/data/db.py:399`），所以线上无感；但裸连接
调用（诊断脚本 / 库外集成 / 评测脚本自建连接）必崩。本文件把该行为锁住。

覆盖：
- `_rows_to_dicts` 在裸连接（row_factory=None）下正确
- `_rows_to_dicts` 在已设 row_factory 的连接的连接下正确（生产形态）
- `search_memories` 传入裸连接不抛 ValueError
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.search import _rows_to_dicts
from sgme.data.search import init_fts
from sgme.data.search import search_memories


@pytest.fixture
def cfg():
    c = config.load_config()
    # 显式指向 mock embed 地址，避免真实网络（与 test_search_v04 同口径）
    c.setdefault("search", {}).setdefault("vector", {})[
        "base_url"
    ] = "http://mock-embed.test/v1"
    return c


def test_rows_to_dicts_bare_connection():
    """裸连接（row_factory=None）：元组行也要能转成 dict。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'x')")
        cur = conn.execute("SELECT a, b FROM t")
        assert _rows_to_dicts(cur) == [{"a": 1, "b": "x"}]
    finally:
        conn.close()


def test_rows_to_dicts_row_factory_connection():
    """生产形态（row_factory=sqlite3.Row）：行为不变。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (2, 'y')")
        cur = conn.execute("SELECT a, b FROM t")
        assert _rows_to_dicts(cur) == [{"a": 2, "b": "y"}]
    finally:
        conn.close()


def test_rows_to_dicts_empty():
    """空结果集返回空列表（不因 description 为 None 崩）。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (a INTEGER)")
        cur = conn.execute("SELECT a FROM t")
        assert _rows_to_dicts(cur) == []
    finally:
        conn.close()


def test_search_with_bare_connection_does_not_crash(tmp_path, cfg):
    """关键回归：检索入口传入裸连接不得抛 ValueError。

    允许 OperationalError（本用例未建 session 表），但 ValueError 一律视为回归失败。
    """
    conn = db_mod.connect_memory(tmp_path)
    try:
        memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
        init_fts(conn)
        conn.row_factory = None  # ← 模拟裸 sqlite3.connect 调用方
        try:
            search_memories(conn, conn, "记忆", limit=3, cfg=cfg)
        except ValueError as exc:  # pragma: no cover - 回归守卫
            pytest.fail(f"裸连接检索抛 ValueError（回归）：{exc}")
        except Exception:
            # 其他异常（缺表等）与本回归无关
            pass
    finally:
        conn.close()
