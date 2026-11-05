"""D-78 惊讶度先验接线回归：surprise 必须能落库，且计算失败不阻塞存储。

两层证据：
1. ``memory_dao.insert_memory`` 的 extra 通路能写 surprise 列（schema 已迁移）
2. ``surprise.compute_for_store`` 是旁路——任何异常返回 None 而不是抛出

接线点在 ``l3_adjudicate._store_memory`` 与 ``scene_aggregate._ensure_persisted`` 两处落库路径；
本用例锁住底层契约，避免「模块写好了但落库字段被吞」的旧坑重演。
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine import surprise as surprise_mod


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    from sgme import config

    cfg = config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _insert(conn: sqlite3.Connection, content: str, memory_type: str = "fact", extra=None) -> str:
    return memory_dao.insert_memory(
        conn,
        content=content,
        memory_type=memory_type,
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=[],
        extra=extra,
    )


def test_extra_writes_surprise(mem_conn):
    """extra 带 surprise → 落库到 memories.surprise。"""
    mid = _insert(mem_conn, "用户偏好中文交流", extra={"surprise": 0.42, "type": "偏好"})
    row = mem_conn.execute(
        "SELECT surprise, type FROM memories WHERE memory_id=?", (mid,)
    ).fetchone()
    assert row[0] == pytest.approx(0.42)
    assert row[1] == "偏好"


def test_surprise_column_null_by_default(mem_conn):
    """不传 extra → surprise 为 NULL（存量语义：未计算，不编造）。"""
    mid = _insert(mem_conn, "一条没有惊讶度的记忆")
    row = mem_conn.execute("SELECT surprise FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row[0] is None


def test_compute_for_store_returns_score(mem_conn):
    """同类型已有 40 条 → compute_for_store 返回 0-1 的分数。"""
    for i in range(40):
        _insert(mem_conn, f"项目进度记录 {i} 号条目内容", memory_type="fact")
    score = surprise_mod.compute_for_store(mem_conn, "用户偏好用中文交流技术细节", "fact")
    assert score is not None
    assert 0.0 <= score <= 1.0


def test_compute_for_store_never_raises(mem_conn):
    """异常输入（None / 空库）不抛错，返回 None 或合法分数。"""
    assert surprise_mod.compute_for_store(mem_conn, None, "fact") is None
    # 空内容在空库上：不抛错即可（返回值允许 None 或数值）
    surprise_mod.compute_for_store(mem_conn, "", "fact")


def test_insert_then_archive_keeps_surprise(mem_conn):
    """归档链保留 surprise 字段（保证溯源可还原 2.0 元数据）。"""
    mid = _insert(mem_conn, "会被归档的记忆", extra={"surprise": 0.77})
    memory_dao.archive_memory(mem_conn, mid, superseded_by="test")
    row = mem_conn.execute(
        "SELECT surprise FROM memory_archive WHERE memory_id=?", (mid,)
    ).fetchone()
    assert row is not None, "归档行未写入"
    assert row[0] == pytest.approx(0.77)
