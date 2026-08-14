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
