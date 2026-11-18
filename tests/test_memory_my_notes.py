"""T-423 · D-30 纠错备注独立列 ``my_notes`` 测试（2026-11-18）。

D-30：人工备注 = 记忆详情页 my_notes 独立列 + 纠错按钮（supersede 链）。
按 AGENTS「改字段四步」锁死行为：

1. 建列幂等：迁移跑两次不报错，PRAGMA table_info 有该列
2. 老库迁移：模拟无列老库 → connect_memory 后补列且旧数据不动
3. 写入：reject 带 my_notes → 落库；None 不覆写既有备注
4. 读取：memory_get / 列表 / 检索三条读取路径透出
5. 不影响既有 reject_reason 语义（不变响应键序、reason 缺省回落不变）

所有测试 tmp_path 隔离。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.operations import memory as ops_memory


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path / "data")
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    try:
        conn.close()
    except Exception:
        pass


def _mk_memory(conn, memory_id: str, content: str = "测试记忆内容") -> str:
    """造一条最小 active 记忆，返回 memory_id。"""
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, status) VALUES (?,?,?,?,?,?,?, 'active')",
        (memory_id, content, "fact", 70, "normal", "2026-11-18T00:00:00Z", "2026-11-18T00:00:00Z"),
    )
    conn.commit()
    return memory_id


# ---------- ① 建列幂等 + 老库迁移 ----------

def test_column_exists_after_connect(mem_conn):
    """新库 connect 后 PRAGMA table_info 有 my_notes 列。"""
    cols = [r[1] for r in mem_conn.execute("PRAGMA table_info(memories)").fetchall()]
    assert "my_notes" in cols


def test_migration_idempotent(tmp_path, cfg):
    """迁移重复执行不报错（幂等）。"""
    for _ in range(2):
        conn = db_mod.connect_memory(tmp_path / "data")
        memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
        conn.close()
    conn = db_mod.connect_memory(tmp_path / "data")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    assert "my_notes" in cols
    conn.close()


def test_old_db_migration_preserves_data(tmp_path):
    """老库（无 my_notes 列）迁移：列补上、既有 reject_reason 数据原样保留。"""
    d = tmp_path / "olddb"
    d.mkdir()
    conn = sqlite3.connect(d / "memory.db")
    conn.row_factory = sqlite3.Row
    # 造一个「未带 my_notes」的 memories 表（手工 DDL，模拟老库）
    conn.execute(
        "CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, "
        "memory_type TEXT NOT NULL, priority INTEGER NOT NULL, time_velocity TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', "
        "rejected_at TEXT, reject_reason TEXT)"
    )
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, status, reject_reason) VALUES "
        "('old-1', '老记忆', 'fact', 70, 'normal', '2026-01-01', '2026-01-01', 'rejected', '用户纠错')"
    )
    conn.commit()
    conn.close()

    conn = db_mod.connect_memory(d)  # 触发迁移
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    assert "my_notes" in cols
    row = conn.execute("SELECT * FROM memories WHERE memory_id='old-1'").fetchone()
    assert row["reject_reason"] == "用户纠错"  # 旧数据不动
    assert row["my_notes"] is None  # 新列 NULL 起步
    conn.close()


# ---------- ② 落库路径 ----------

def test_reject_writes_my_notes(mem_conn):
    """reject 带 my_notes → 落库，reject_reason 语义不变。"""
    mid = _mk_memory(mem_conn, "m-notes-1")
    out = ops_memory.reject_memory(mem_conn, mid, reason="日期记错了", my_notes="正确日期是 11-20")
    assert out.ok
    row = mem_conn.execute("SELECT * FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "日期记错了"  # 既有语义不变
    assert row["my_notes"] == "正确日期是 11-20"


def test_reject_none_preserves_existing_notes(mem_conn):
    """重复 reject 不带 my_notes → 既有备注保留（None 不覆写）。"""
    mid = _mk_memory(mem_conn, "m-notes-2")
    ops_memory.reject_memory(mem_conn, mid, reason="第一次", my_notes="备注 v1")
    ops_memory.reject_memory(mem_conn, mid, reason="第二次", my_notes=None)
    row = mem_conn.execute("SELECT my_notes, reject_reason FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row["my_notes"] == "备注 v1"
    assert row["reject_reason"] == "第二次"  # reason 照旧更新


def test_default_reject_without_notes(mem_conn):
    """不带 my_notes 的 reject：reason 缺省回落不变，my_notes 保持 NULL。"""
    mid = _mk_memory(mem_conn, "m-notes-3")
    out = ops_memory.reject_memory(mem_conn, mid)
    assert out.ok
    assert out.data["reject_reason"] == "用户纠错"
    row = mem_conn.execute("SELECT my_notes FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row["my_notes"] is None


def test_purge_snapshot_includes_my_notes(mem_conn):
    """purge 审计快照（SELECT *）包含 my_notes——彻底删除后备注可溯。"""
    mid = _mk_memory(mem_conn, "m-notes-4")
    ops_memory.reject_memory(mem_conn, mid, reason="x", my_notes="含敏感内容，彻底删除")
    out = memory_dao.purge_memory(mem_conn, mid, triggered_by="user", reason="测试")
    assert out is not None
    assert out["snapshot"]["memory"]["my_notes"] == "含敏感内容，彻底删除"


# ---------- ③ 读取路径 ----------

def test_memory_get_returns_my_notes(mem_conn):
    """memory_get 详情返回 my_notes（DAO SELECT * 透传，data.memory 嵌套）。"""
    mid = _mk_memory(mem_conn, "m-notes-5")
    ops_memory.reject_memory(mem_conn, mid, reason="r", my_notes="详情页备注")
    out = ops_memory.get_memory(mem_conn, mid)
    assert out.ok
    assert out.data["memory"]["my_notes"] == "详情页备注"


def test_browse_list_returns_my_notes(mem_conn):
    """管理面列表（list_memories_page）透出 my_notes（列缺失时补 null 占位）。"""
    mid = _mk_memory(mem_conn, "m-notes-6")
    ops_memory.reject_memory(mem_conn, mid, reason="r", my_notes="列表备注")
    items, _total = memory_dao.list_memories_page(
        mem_conn, page=1, limit=10, statuses=("active", "rejected"))
    found = {it["memory_id"]: it for it in items}
    assert found[mid]["my_notes"] == "列表备注"


def test_search_returns_my_notes(mem_conn, tmp_path):
    """检索返回（_enrich_l2_context 回填）透出 my_notes——无数据为 None 不报错。

    注：reject 会把记忆置 rejected（检索本就过滤 rejected，既有可见性语义），
    故本例直接在 active 行上写备注，只测「检索返回带 my_notes 键」这一读取路径。
    """
    mid = _mk_memory(mem_conn, "m-notes-7", content="唯一的检索命中词青蒿素")
    mem_conn.execute("UPDATE memories SET my_notes=? WHERE memory_id=?", ("检索备注", mid))
    mem_conn.commit()
    session_conn, _, _ = db_mod.init_databases(tmp_path / "sdata")
    try:
        results = db_mod_search(mem_conn, session_conn, "青蒿素")
    finally:
        session_conn.close()
    hit = next(r for r in results if r["memory_id"] == mid)
    assert hit["my_notes"] == "检索备注"


def db_mod_search(mem_conn, session_conn, query: str) -> list[dict]:
    from sgme.data.search import search_memories
    return search_memories(mem_conn, session_conn, query, limit=5)
