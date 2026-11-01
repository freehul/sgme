"""连接参数回归：SQLite busy_timeout 必须生效。

背景（2026-11-01 生产取证）：`_connect` 只设了 WAL 与外键，**没有设 busy_timeout**。
SQLite 默认值为 0，写锁被占用时立即抛 ``database is locked`` 而非等待——
生产日志累计 948 条锁错误并出现过「未处理异常: database is locked」的 500。
本用例锁住该设置，去掉 PRAGMA 即红。
"""
from __future__ import annotations

from sgme.data import db as db_mod


def test_memory_conn_sets_busy_timeout(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_session_conn_sets_busy_timeout(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_wal_mode_still_enabled(tmp_path):
    """顺带守住 WAL——busy_timeout 的等待只在 WAL 下才有意义（读写不互斥）。"""
    conn = db_mod.connect_memory(tmp_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()
