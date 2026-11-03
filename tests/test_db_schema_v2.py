# -*- coding: utf-8 -*-
"""tests/test_db_schema_v2.py：SGME 2.0 迁移阶段 1（schema v5）结构回归。

A. 新库结构：memories/memory_archive 五列、dimension_registry.priority、四张 2.0
   正式表（ttl_disposition 含 reason / deletion_audit / rule_sync_ledger /
   rule_sync_daily）、wiki_pages 九列 + wiki_revisions/wiki_categories、凭据四列。
B. 老库升级：先建 v5 再由 ALTER DROP 回退成"缺列形态"，重连后迁移必须补回并回填
   （status=expired → sleeping + inject_tier='none'；归档表 → archived；其余 → active），
   且二次重连不改写既有值（幂等）。
"""
from __future__ import annotations

from sgme.data import credentials_dao, db

MEM_COLS = ("lifecycle", "ttl_renew_count", "ttl_renewed_at", "inject_tier", "meaning_anchor")
WIKI_COLS = (
    "page_type",
    "headline",
    "content_hash",
    "heat_score",
    "last_recalled_at",
    "view_count",
    "dirty",
    "updated_by",
    "my_notes",
)
CRED_COLS = ("scope", "cred_type", "expires_at", "last_used_at")


def _cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _tables(conn):
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _insert_memory(conn, memory_id: str, status: str = "active", reject_reason: str | None = None):
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, status, reject_reason) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            memory_id,
            "content-" + memory_id,
            "fact",
            50,
            "static",
            "2026-10-01T00:00:00Z",
            "2026-10-01T00:00:00Z",
            status,
            reject_reason,
        ),
    )


def test_fresh_memory_schema_has_2_0_columns_and_tables(tmp_path):
    conn = db.connect_memory(tmp_path)
    for table in ("memories", "memory_archive"):
        cols = _cols(conn, table)
        for col in MEM_COLS:
            assert col in cols, f"{table} 缺 2.0 列 {col}"
    assert "priority" in _cols(conn, "dimension_registry"), "dimension_registry 缺 priority（D-53 R1）"

    tables = _tables(conn)
    for table in ("ttl_disposition", "deletion_audit", "rule_sync_ledger", "rule_sync_daily"):
        assert table in tables, f"缺 2.0 正式表 {table}"
    assert "reason" in _cols(conn, "ttl_disposition"), "ttl_disposition 缺 reason 列（D-73 加固来源）"
    assert conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_legacy_memory_db_upgrade_backfills_lifecycle(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert_memory(conn, "m-active")
    _insert_memory(conn, "m-expired", status="expired", reject_reason="dream_ttl_expired")
    conn.execute(
        "INSERT INTO memory_archive (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, archived_at) VALUES (?,?,?,?,?,?,?,?)",
        ("m-old", "old-content", "fact", 50, "static",
         "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"),
    )
    conn.commit()

    # 回退成 1.x 形态：删索引 → 删 2.0 列 → 删 2.0 表
    conn.execute("DROP INDEX IF EXISTS idx_mem_lifecycle")
    for col in MEM_COLS:
        conn.execute(f"ALTER TABLE memories DROP COLUMN {col}")
        conn.execute(f"ALTER TABLE memory_archive DROP COLUMN {col}")
    conn.execute("ALTER TABLE dimension_registry DROP COLUMN priority")
    for table in ("ttl_disposition", "deletion_audit", "rule_sync_ledger", "rule_sync_daily"):
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()

    conn = db.connect_memory(tmp_path)
    for table in ("memories", "memory_archive"):
        for col in MEM_COLS:
            assert col in _cols(conn, table), f"迁移后 {table} 仍缺 {col}"
    assert "priority" in _cols(conn, "dimension_registry")
    for table in ("ttl_disposition", "deletion_audit", "rule_sync_ledger", "rule_sync_daily"):
        assert table in _tables(conn)

    rows = dict(conn.execute("SELECT memory_id, lifecycle FROM memories").fetchall())
    assert rows["m-active"] == "active"
    assert rows["m-expired"] == "sleeping", "status=expired 专则应映射 sleeping（§9.3 专则优先）"
    assert (
        conn.execute("SELECT inject_tier FROM memories WHERE memory_id='m-expired'").fetchone()[0]
        == "none"
    )
    assert conn.execute("SELECT lifecycle FROM memory_archive").fetchone()[0] == "archived"

    # 幂等：迁移只回填 NULL，不覆盖已有值
    conn.execute("UPDATE memories SET lifecycle='archived' WHERE memory_id='m-active'")
    conn.commit()
    conn.close()
    conn = db.connect_memory(tmp_path)
    assert (
        conn.execute("SELECT lifecycle FROM memories WHERE memory_id='m-active'").fetchone()[0]
        == "archived"
    )
    conn.close()


def test_wiki_2_0_schema_and_legacy_upgrade(tmp_path):
    conn = db.connect_wiki(tmp_path)
    for col in WIKI_COLS:
        assert col in _cols(conn, "wiki_pages"), f"wiki_pages 缺 2.0 列 {col}"
    assert {"wiki_revisions", "wiki_categories"} <= _tables(conn)
    conn.execute("INSERT INTO wiki_pages (page_id, title, content, status) VALUES ('p1','t','c','active')")
    conn.commit()

    # 回退成 1.x 形态
    conn.execute("DROP INDEX IF EXISTS idx_wiki_pages_type")
    conn.execute("DROP INDEX IF EXISTS idx_wiki_pages_hash")
    for col in WIKI_COLS:
        conn.execute(f"ALTER TABLE wiki_pages DROP COLUMN {col}")
    conn.execute("DROP TABLE wiki_revisions")
    conn.execute("DROP TABLE wiki_categories")
    conn.commit()
    conn.close()

    conn = db.connect_wiki(tmp_path)
    for col in WIKI_COLS:
        assert col in _cols(conn, "wiki_pages")
    assert {"wiki_revisions", "wiki_categories"} <= _tables(conn)
    assert conn.execute("SELECT page_type FROM wiki_pages WHERE page_id='p1'").fetchone()[0] == "topic"
    conn.close()


def test_credentials_2_0_columns_and_legacy_upgrade(tmp_path):
    conn = credentials_dao.connect_credentials(tmp_path)
    for col in CRED_COLS:
        assert col in _cols(conn, "credentials"), f"credentials 缺 2.0 列 {col}"
    assert conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == (
        credentials_dao.SCHEMA_VERSION
    )
    conn.execute(
        "INSERT INTO credentials (key_name, value_cipher, created_at, updated_at, updated_by) "
        "VALUES ('k1','cipher','t','t','test')"
    )
    conn.commit()
    for col in CRED_COLS:
        conn.execute(f"ALTER TABLE credentials DROP COLUMN {col}")
    conn.commit()
    conn.close()

    conn = credentials_dao.connect_credentials(tmp_path)
    for col in CRED_COLS:
        assert col in _cols(conn, "credentials")
    assert conn.execute("SELECT key_name FROM credentials").fetchone()[0] == "k1"
    conn.close()
