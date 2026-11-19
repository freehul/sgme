# -*- coding: utf-8 -*-
"""tests/test_scene_retire_scripts.py：场景四表退役/回滚脚本回归（T-427）。

覆盖：
1. 退役 dry-run 零写入；
2. 退役顺序正确：清触发器/FTS → 子表 rename → 主表 rename；
3. 数据只 rename 不 drop（行数守恒）、memory_edges 场景边打标；
4. 回滚：表名恢复 + content_seg 回填 + scenes_fts/触发器重建 + rebuild 行数一致；
5. 外键校验 0 违规。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sgme.data.db import SCENE_RETIRED_DDL

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file_name)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


retire_mod = _load("retire_scene_tables_under_test", "retire_scene_tables.py")
rollback_mod = _load("rollback_scene_tables_under_test", "rollback_scene_tables.py")

SCENES_FTS_DDL = """
CREATE VIRTUAL TABLE scenes_fts USING fts5(
    content_seg,
    scene_id UNINDEXED,
    content='scenes',
    content_rowid='rowid'
);
"""
SCENES_FTS_TRIGGERS = """
CREATE TRIGGER scenes_ai AFTER INSERT ON scenes BEGIN
    INSERT INTO scenes_fts(rowid, content_seg, scene_id)
    VALUES (new.rowid, new.content_seg, new.scene_id);
END;
CREATE TRIGGER scenes_ad AFTER DELETE ON scenes BEGIN
    INSERT INTO scenes_fts(scenes_fts, rowid, content_seg, scene_id)
    VALUES ('delete', old.rowid, old.content_seg, old.scene_id);
END;
CREATE TRIGGER scenes_au AFTER UPDATE ON scenes BEGIN
    INSERT INTO scenes_fts(scenes_fts, rowid, content_seg, scene_id)
    VALUES ('delete', old.rowid, old.content_seg, old.scene_id);
    INSERT INTO scenes_fts(rowid, content_seg, scene_id)
    VALUES (new.rowid, new.content_seg, new.scene_id);
END;
"""


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SCENE_RETIRED_DDL)
        conn.executescript(
            """
            CREATE TABLE memory_edges (
              edge_id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT, relation TEXT,
              weight REAL, valid_from TEXT, valid_to TEXT, created_at TEXT, source TEXT);
            """
        )
        conn.execute(
            "INSERT INTO scenes (scene_id, title, content, heat, status, created_at, updated_at) "
            "VALUES ('s1','场景一','正文一',2,'active','2026-01-01T00:00:00Z','2026-01-02T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO scenes (scene_id, title, content, heat, status, created_at, updated_at) "
            "VALUES ('s2','场景二','正文二',1,'archived','2026-01-01T00:00:00Z','2026-01-02T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO scene_vectors (scene_id, embedding, model, dims, embedded_at) "
            "VALUES ('s1', X'00', 'm', 1, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO scene_memories (scene_id, memory_id) VALUES ('s1','m1')"
        )
        conn.execute(
            "INSERT INTO scene_versions (version_id, scene_id, content, version_at, reason) "
            "VALUES ('v1','s1','旧正文','2026-01-01T00:00:00Z','update')"
        )
        conn.execute(
            "INSERT INTO memory_edges VALUES "
            "('m1::m2::belongs_to','m1','m2','belongs_to',1.0,NULL,NULL,'2026-01-01','system')"
        )
        conn.execute(
            "INSERT INTO memory_edges VALUES "
            "('A::B::supersedes','A','B','supersedes',1.0,NULL,NULL,'2026-01-01','system')"
        )
        conn.executescript(SCENES_FTS_DDL)
        conn.executescript(SCENES_FTS_TRIGGERS)
        conn.execute("INSERT INTO scenes_fts(scenes_fts) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()
    return db


def _tables(conn) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def test_retire_dry_run_no_write(tmp_path):
    db = _make_db(tmp_path)
    before = db.read_bytes()
    report = retire_mod.retire(db, apply=False)
    assert report["mode"] == "dry_run"
    assert report["plan"]
    assert db.read_bytes() == before


def test_retire_and_rollback_roundtrip(tmp_path):
    db = _make_db(tmp_path)
    report = retire_mod.retire(db, apply=True)
    assert report["mode"] == "apply"
    assert report["foreign_key_violations"] == 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        names = _tables(conn)
        for t in ("scenes", "scene_vectors", "scene_memories", "scene_versions"):
            assert t not in names
            assert f"{t}_retired" in names
        assert "scenes_fts" not in names
        triggers = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'scenes_%'"
            )
        }
        assert triggers == set()
        # 数据只 rename 不 drop
        assert conn.execute("SELECT COUNT(*) FROM scenes_retired").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM scene_memories_retired").fetchone()[0] == 1
        # 场景边打标，supersession 边不动
        rows = {
            r["relation"]: r["source"]
            for r in conn.execute("SELECT relation, source FROM memory_edges")
        }
        assert rows["belongs_to"] == "scene"
        assert rows["supersedes"] == "system"
    finally:
        conn.close()

    rb = rollback_mod.rollback(db, apply=True)
    assert rb["mode"] == "apply"
    assert rb["foreign_key_violations"] == 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        names = _tables(conn)
        for t in ("scenes", "scene_vectors", "scene_memories", "scene_versions"):
            assert t in names
        assert "scenes_fts" in names
        triggers = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'scenes_%'"
            )
        }
        assert triggers == {"scenes_ai", "scenes_ad", "scenes_au"}
        assert conn.execute("SELECT COUNT(*) FROM scenes_fts").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM scenes WHERE content_seg IS NULL"
        ).fetchone()[0] == 0
        rows = {
            r["relation"]: r["source"]
            for r in conn.execute("SELECT relation, source FROM memory_edges")
        }
        assert rows["belongs_to"] == "system"
    finally:
        conn.close()


def test_retire_idempotent(tmp_path):
    db = _make_db(tmp_path)
    retire_mod.retire(db, apply=True)
    again = retire_mod.retire(db, apply=True)
    assert any("already_retired" in a for a in again["actions"])
