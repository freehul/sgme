# -*- coding: utf-8 -*-
"""tests/test_triage_columns.py：T9 候选三分类列（suggested_kind / suggested_domain）。

验收（任务清单-候选三分类-20261115.md T9）：
1. 迁移幂等：跑两次不报错、列存在（新库 DDL 自带；老库经迁移函数补齐）；
2. 写回函数：set_candidate_naming 写 suggested_kind / suggested_domain 后能读回
   （与 suggested_name / naming_model / named_at 一并原子落库）。

kind 取值：skill（可执行，进技能库）/ wiki（可查证，进知识库）/ discard（不沉淀）。
domain 取值：24 领域白名单 ∪ {other}（校验在 apply_skill_names.py，列本身不设约束）。
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def skills_db(tmp_path):
    from sgme.data import db as db_mod

    conn = db_mod.connect_skills(tmp_path)
    yield conn
    conn.close()


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(skill_candidates)").fetchall()}


def _insert_candidate(conn: sqlite3.Connection, memory_id: str) -> None:
    now = "2026-11-15T00:00:00Z"
    conn.execute(
        "INSERT INTO skill_candidates (memory_id, name, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (memory_id, "exp-test0001", "accepted", now, now),
    )
    conn.commit()


def test_fresh_db_has_triage_columns(skills_db):
    cols = _columns(skills_db)
    assert {"suggested_kind", "suggested_domain"} <= cols


def test_migration_idempotent_on_old_schema(tmp_path):
    """老库（无三分类列）迁移补列；重复跑两次不报错、不重复加列。"""
    from sgme.data import db as db_mod

    conn = sqlite3.connect(tmp_path / "skills.db")
    conn.executescript(
        """
        CREATE TABLE skill_candidates (
          candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
          memory_id TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          category TEXT, tags TEXT,
          confidence REAL NOT NULL DEFAULT 0.0,
          status TEXT NOT NULL DEFAULT 'pending',
          reason TEXT, note TEXT, reviewer TEXT,
          source TEXT NOT NULL DEFAULT 'memory_route',
          content_sha256 TEXT,
          suggested_name TEXT, naming_model TEXT, named_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reviewed_at TEXT);
        """
    )
    conn.commit()
    first = db_mod._migrate_skill_candidates_triage_columns(conn)
    second = db_mod._migrate_skill_candidates_triage_columns(conn)
    assert first is True and second is False  # 第二次无列可补
    assert {"suggested_kind", "suggested_domain"} <= _columns(conn)
    conn.close()


def test_set_candidate_naming_writes_triage(skills_db):
    """写回函数：三件套 + 三分类一并落库，读回一致。"""
    from sgme.data import skill_candidates_dao

    _insert_candidate(skills_db, "m-1")
    ok = skill_candidates_dao.set_candidate_naming(
        skills_db, "m-1",
        name="h3-int8-load-fail-use-gguf-q4",
        model="zcode-glm",
        named_at="2026-11-15T03:00:00Z",
        kind="skill",
        domain="ai",
    )
    assert ok is True
    row = skills_db.execute(
        "SELECT suggested_name, naming_model, named_at, suggested_kind, suggested_domain"
        " FROM skill_candidates WHERE memory_id='m-1'"
    ).fetchone()
    assert row["suggested_name"] == "h3-int8-load-fail-use-gguf-q4"
    assert row["naming_model"] == "zcode-glm"
    assert row["named_at"] == "2026-11-15T03:00:00Z"
    assert row["suggested_kind"] == "skill"
    assert row["suggested_domain"] == "ai"


def test_set_candidate_naming_triage_optional(skills_db):
    """不传 kind/domain 时列保持 NULL（向后兼容 v2 纯命名路径）。"""
    from sgme.data import skill_candidates_dao

    _insert_candidate(skills_db, "m-2")
    skill_candidates_dao.set_candidate_naming(
        skills_db, "m-2", name="some-name", model="zcode-glm",
        named_at="2026-11-15T03:00:00Z",
    )
    row = skills_db.execute(
        "SELECT suggested_kind, suggested_domain FROM skill_candidates WHERE memory_id='m-2'"
    ).fetchone()
    assert row["suggested_kind"] is None and row["suggested_domain"] is None
