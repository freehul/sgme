"""tests/test_migrations.py：v0.7 三库拆分迁移脚本测试（副本演练自动化）。

覆盖 migrations/0001_split_three_dbs.up()：
1. 全表搬运正确（raw_files → session.db；scenes 系列 → memory.db）
2. 源库表保留（D5：不 DROP，作归档）
3. 可重入（INSERT OR IGNORE，第二次执行行数不变）
4. 备份失败立即中止（MigrationAbort，零搬运）
5. scenes_fts 重建成功（迁移后 FTS 可检索）
6. 历史空文件 data/sgme.db 清理
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sgme.data import db as db_mod
from migrations import _registry

# 旧 wiki.db 结构（v0.7 前：raw_files + scenes 系列；列清单与 migrations/_move_data.move 一致）
LEGACY_WIKI_DDL = """
CREATE TABLE raw_files (
    file_id TEXT PRIMARY KEY, path TEXT NOT NULL, session_key TEXT NOT NULL,
    agent_id TEXT, started_at TEXT, ended_at TEXT, refined_at TEXT,
    last_refined_seq INTEGER, status TEXT DEFAULT 'new', size INTEGER, content_hash TEXT
);
CREATE TABLE scenes (
    scene_id TEXT PRIMARY KEY, title TEXT, content TEXT, heat REAL,
    status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT,
    last_memory_added_at TEXT, content_seg TEXT
);
CREATE TABLE scene_vectors (
    scene_id TEXT PRIMARY KEY, embedding BLOB, model TEXT, dims INTEGER, embedded_at TEXT
);
CREATE TABLE scene_memories (
    scene_id TEXT, memory_id TEXT, PRIMARY KEY (scene_id, memory_id)
);
CREATE TABLE scene_versions (
    version_id TEXT PRIMARY KEY, scene_id TEXT, content TEXT, version_at TEXT, reason TEXT
);
CREATE TABLE fts_meta (key TEXT PRIMARY KEY, value TEXT);
"""


@pytest.fixture
def legacy_env(tmp_path):
    """构造旧结构双库（wiki.db 背 raw_files+scenes 系列；memory/session 为新 DDL）。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    mem = db_mod.connect_memory(data_dir)
    session = db_mod.connect_session(data_dir)

    w = sqlite3.connect(data_dir / "wiki.db")
    w.row_factory = sqlite3.Row
    w.executescript(LEGACY_WIKI_DDL)
    w.executemany(
        "INSERT INTO raw_files (file_id, path, session_key, agent_id, started_at,"
        " ended_at, refined_at, last_refined_seq, status, size, content_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("f1", "raw/sessions/f1.md", "s1", None, "2026-01-01T00:00:00Z",
             None, None, 0, "new", 100, None),
            ("f2", "raw/sessions/f2.md", "s2", None, "2026-01-02T00:00:00Z",
             None, None, 0, "refined", 200, "abc"),
        ],
    )
    w.executemany(
        "INSERT INTO scenes (scene_id, title, content, heat, status, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?)",
        [
            ("sc1", "测试场景", "内容1", 5, "active",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ("sc2", "场景二", "内容2", 1, "active",
             "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z"),
        ],
    )
    w.executemany(
        "INSERT INTO scene_vectors (scene_id, embedding, model, dims, embedded_at)"
        " VALUES (?,?,?,?,?)",
        [("sc1", sqlite3.Binary(b"vec1"), "test", 4, "2026-01-01T00:00:00Z")],
    )
    w.executemany(
        "INSERT INTO scene_memories (scene_id, memory_id) VALUES (?,?)",
        [("sc1", "m1"), ("sc2", "m2")],
    )
    w.executemany(
        "INSERT INTO scene_versions (version_id, scene_id, content, version_at,"
        " reason) VALUES (?,?,?,?,?)",
        [("v1", "sc1", "旧版", "2026-01-01T00:00:00Z", "merge")],
    )
    w.execute("INSERT INTO fts_meta (key, value) VALUES (?,?)", ("segmenter_scenes", "jieba"))
    w.commit()

    # 历史误建空文件（迁移应清理）
    (data_dir / "sgme.db").write_bytes(b"")

    yield data_dir, mem, session, w
    db_mod.close(mem)
    db_mod.close(session)
    w.close()


def _conns(data_dir, mem, session, w):
    return {"data_dir": data_dir, "memory": mem, "session": session, "wiki": w}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _migration():
    return _registry.load_migrations()[0]


def test_up_moves_all_tables(legacy_env):
    data_dir, mem, session, w = legacy_env
    mig = _migration()
    assert mig.version == 701

    result = mig.up(_conns(data_dir, mem, session, w))

    # 1) 数据搬齐
    assert _count(session, "raw_files") == 2
    assert _count(mem, "scenes") == 2
    assert _count(mem, "scene_vectors") == 1
    assert _count(mem, "scene_memories") == 2
    assert _count(mem, "scene_versions") == 1
    # 2) 源库保留（D5 归档不 DROP）
    assert _count(w, "raw_files") == 2
    assert _count(w, "scenes") == 2
    # 3) fts_meta 合并
    assert _count(mem, "fts_meta") >= 1
    # 4) 历史空文件清理
    assert not (data_dir / "sgme.db").exists()
    # 5) 快照存在
    snap_dir = data_dir / "backups" / "pre_v07"
    assert snap_dir.is_dir()
    assert any(snap_dir.glob("pre_v07_*"))
    # 6) 摘要结构
    assert "pre_snapshot" in result and "moved" in result
    assert result["post_broken"] == 0


def test_up_rebuilds_scenes_fts(legacy_env):
    data_dir, mem, session, w = legacy_env
    _migration().up(_conns(data_dir, mem, session, w))

    # FTS 重建后行数与 scenes 一致（按新 rowid 重绑）
    fts_rows = mem.execute("SELECT COUNT(*) FROM scenes_fts").fetchone()[0]
    assert fts_rows == _count(mem, "scenes") == 2


def test_up_idempotent(legacy_env):
    data_dir, mem, session, w = legacy_env
    conns = _conns(data_dir, mem, session, w)
    _migration().up(conns)
    before = (_count(session, "raw_files"), _count(mem, "scenes"))
    _migration().up(conns)  # 第二次执行
    after = (_count(session, "raw_files"), _count(mem, "scenes"))
    assert before == after == (2, 2)


def test_up_aborts_on_backup_failure(legacy_env, monkeypatch):
    from sgme.backup import manager as backup_mod
    from migrations._move_data import move as real_move
    from migrations import _registry as reg

    data_dir, mem, session, w = legacy_env

    def boom(*args, **kwargs):
        raise RuntimeError("磁盘故障")

    monkeypatch.setattr(backup_mod, "create_snapshot", boom)

    with pytest.raises(RuntimeError, match="磁盘故障"):
        _migration().up(_conns(data_dir, mem, session, w))

    # 零搬运
    assert _count(session, "raw_files") == 0
    assert _count(mem, "scenes") == 0
    # 源库原封不动
    assert _count(w, "raw_files") == 2
    assert _count(w, "scenes") == 2
    # 空文件未清理（迁移未完成）
    assert (data_dir / "sgme.db").exists()


# ---------- L1.5 T1.2：隔离表 DDL + refine_runs 候选留痕三列（2026-10-09） ----------

# 老库 refine_runs 结构（无 candidate_count / candidate_ids_hash / short_circuit_reason 三列）
LEGACY_REFINE_RUNS_DDL = """
CREATE TABLE refine_runs (
    run_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, stage TEXT NOT NULL,
    version TEXT NOT NULL, variant TEXT, provider TEXT NOT NULL,
    bucket_key TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    memories_count INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
    action_counts TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'running',
    error TEXT);
CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
INSERT INTO schema_versions VALUES (3, 'memory_v3', '2026-09-01T00:00:00Z');
"""


def test_legacy_refine_runs_migration_adds_candidate_cols(tmp_path):
    """老库（refine_runs 缺三列）→ connect_memory 自动 ALTER 补列（幂等）。"""
    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    conn.executescript(LEGACY_REFINE_RUNS_DDL)
    conn.execute(
        "INSERT INTO refine_runs (run_id, file_id, stage, version, provider, bucket_key, started_at)"
        " VALUES ('r1','f1','l1_conflict','v001','lm-studio','f1','2026-09-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    upgraded = db_mod.connect_memory(tmp_path)
    try:
        cols = [r[1] for r in upgraded.execute("PRAGMA table_info(refine_runs)").fetchall()]
        assert "candidate_count" in cols
        assert "candidate_ids_hash" in cols
        assert "short_circuit_reason" in cols
        # 老数据行保留，新列为 NULL
        row = upgraded.execute("SELECT * FROM refine_runs WHERE run_id='r1'").fetchone()
        assert row["provider"] == "lm-studio"
        assert row["candidate_count"] is None
        assert row["candidate_ids_hash"] is None
        assert row["short_circuit_reason"] is None
        # 重复迁移（幂等）不报错
        db_mod._migrate_refine_runs_candidate_cols(upgraded)
        db_mod._migrate_refine_runs_candidate_cols(upgraded)
    finally:
        upgraded.close()

    # 二次连接（老库现已被补列）不报错、列仍在
    again = db_mod.connect_memory(tmp_path)
    try:
        cols2 = [r[1] for r in again.execute("PRAGMA table_info(refine_runs)").fetchall()]
        assert {"candidate_count", "candidate_ids_hash", "short_circuit_reason"} <= set(cols2)
    finally:
        again.close()


def test_memory_ddl_contains_l15_objects():
    """DDL 完整性：隔离表/双索引/refine_runs 三列均在 MEMORY_DDL 原文（新库随启动全量生效）。"""
    m = db_mod.MEMORY_DDL
    assert all(c in m for c in (
        "candidate_count", "candidate_ids_hash", "short_circuit_reason",
        "near_dup_quarantine", "idx_quarantine_matched", "idx_quarantine_status",
    ))


def test_connect_memory_creates_quarantine_table(tmp_path):
    """新库连接后隔离表与双索引实际存在（CREATE TABLE IF NOT EXISTS，无需迁移函数）。"""
    conn = db_mod.connect_memory(tmp_path)
    try:
        assert "near_dup_quarantine" in set(db_mod.list_tables(conn))
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
        assert {"idx_quarantine_matched", "idx_quarantine_status"} <= idx
    finally:
        conn.close()


# ---------- T-251：记忆调用统计表 memory_usage_daily（2026-10-10） ----------


def test_legacy_memory_usage_table_migration(tmp_path):
    """老库（无 memory_usage_daily）→ connect_memory 自动补建表 + 双索引（幂等）。

    模拟生产老库形态：schema_versions 已登记 memory_v4、无新表——
    `_ensure_schema` 不会重跑新表 DDL，必须由 `_migrate_memory_usage_table` 补齐。
    """
    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    conn.execute(
        "CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_versions VALUES (4, 'memory_v4', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    upgraded = db_mod.connect_memory(tmp_path)
    try:
        assert "memory_usage_daily" in set(db_mod.list_tables(upgraded))
        idx = {r["name"] for r in upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
        assert {"idx_mem_usage_memory", "idx_mem_usage_day"} <= idx
        # 重复迁移（幂等）不报错
        db_mod._migrate_memory_usage_table(upgraded)
        db_mod._migrate_memory_usage_table(upgraded)
    finally:
        upgraded.close()

    # 二次连接（老库已被补表）不报错、表仍在
    again = db_mod.connect_memory(tmp_path)
    try:
        assert "memory_usage_daily" in set(db_mod.list_tables(again))
    finally:
        again.close()
