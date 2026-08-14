"""memory_stats sidecar DAO 测试（v0.7 阶段 1 新增，A2 决策）。

验证：
1. record_inject 幂等 upsert：首次写入 recall_count=1 + last_injected_at；
   重复注入 recall_count 自增、last_injected_at 刷新。
2. get_stats 正确读回统计；不存在返回 None。
3. best-effort：统计失败（表缺失）不抛异常（已吞异常，返回 False），不阻塞主流程。
4. memory_stats 物理位于 memory.db，与 memories 同库。
"""

import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import memory_stats_dao


@pytest.fixture
def mem(tmp_path):
    """memory.db 连接 + 一条 memories 种子行。

    memory_stats.memory_id 带外键 `REFERENCES memories(memory_id)`（设计文档 §8.2 硬性要求），
    且连接开启 `PRAGMA foreign_keys=ON`，故 record_inject 的目标记忆必须真实存在，
    否则触发 FOREIGN KEY constraint failed（被 best-effort 吞掉 → 返回 False）。
    """
    conn = db_mod.connect_memory(tmp_path)
    conn.execute(
        "INSERT INTO memories("
        "memory_id, content, memory_type, priority, time_velocity, created_at, updated_at) "
        "VALUES ('m1', '测试记忆', 'fact', 3, 'static', "
        "'2026-08-20T00:00:00Z', '2026-08-20T00:00:00Z')"
    )
    conn.commit()
    yield conn
    conn.close()


def test_memory_stats_table_in_memory_db(tmp_path):
    mem_conn = db_mod.connect_memory(tmp_path)
    tables = set(db_mod.list_tables(mem_conn))
    mem_conn.close()
    assert "memory_stats" in tables


def test_record_inject_first_write(mem):
    ok = memory_stats_dao.record_inject(mem, "m1", injected_at="2026-08-20T00:00:00Z")
    assert ok
    stats = memory_stats_dao.get_stats(mem, "m1")
    assert stats is not None
    assert stats["memory_id"] == "m1"
    assert stats["recall_count"] == 1
    assert stats["last_injected_at"] == "2026-08-20T00:00:00Z"


def test_record_inject_idempotent_increment(mem):
    memory_stats_dao.record_inject(mem, "m1", injected_at="2026-08-20T00:00:00Z")
    memory_stats_dao.record_inject(mem, "m1", injected_at="2026-08-20T01:00:00Z")
    stats = memory_stats_dao.get_stats(mem, "m1")
    assert stats["recall_count"] == 2
    assert stats["last_injected_at"] == "2026-08-20T01:00:00Z"


def test_get_stats_missing(mem):
    assert memory_stats_dao.get_stats(mem, "nonexistent") is None


def test_record_inject_default_timestamp(mem):
    ok = memory_stats_dao.record_inject(mem, "m1")
    assert ok
    stats = memory_stats_dao.get_stats(mem, "m1")
    assert stats["last_injected_at"]  # 非空（缺省取当前时间）


def test_record_inject_best_effort_on_missing_table():
    """表缺失时 record_inject 返回 False 且不抛异常（best-effort 语义）。"""
    conn = sqlite3.connect(":memory:")
    try:
        # memory_stats 表不存在 → 应走 except 分支返回 False，不抛
        ok = memory_stats_dao.record_inject(conn, "m1")
        assert ok is False
    finally:
        conn.close()
