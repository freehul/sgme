"""技能删除贯通检索层（2026-11-06）：软删降权 / 硬删清库 / 索引状态映射。

背景（实测缺口）：
① `remove_skill(hard=False)` 只在 SKILL.md 写 `deprecated: true`，索引层不解析
   → `skills.status` 恒为 active → 已归档技能继续霸占检索首位；
② `remove_skill(hard=True)` 只删目录，`skills` / `skill_vectors` / `skill_uses`
   行要等下一轮 `sync_index` 才消失。

本文件锁住改造后的行为：frontmatter → status、软删置 deprecated、硬删清三表、
检索与列表默认不召回停用件、按名仍可取全文（可恢复/可追溯）。
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import skills_dao
from sgme.skills import indexer


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect_skills(tmp_path)
    yield c
    c.close()


def _rec(name: str, content: str = "# body", status: str = "active", uses=None) -> dict:
    return {
        "name": name,
        "sha256": "sha-" + name,
        "description": f"{name} 描述",
        "tags": ["skill"],
        "category": "test",
        "version": "1.0.0",
        "pattern": "manual",
        "source": "git",
        "origin_path": f"/tmp/{name}/SKILL.md",
        "content": content,
        "status": status,
        "uses": uses or [],
    }


# ---------- 1. frontmatter → status ----------


def test_deprecated_flag_maps_to_deprecated_status():
    rec = indexer._record_from_meta(
        "docx", {"deprecated": True, "description": "d"}, "# docx", "git", "/tmp/docx/SKILL.md"
    )
    assert rec.status == "deprecated"


def test_status_field_and_invalid_value():
    assert indexer._record_from_meta(
        "a", {"status": "tombstone"}, "# a", "git", "/tmp/a"
    ).status == "tombstone"
    # 非法值回落 active，不炸
    assert indexer._record_from_meta(
        "b", {"status": "weird"}, "# b", "git", "/tmp/b"
    ).status == "active"
    assert indexer._record_from_meta(
        "c", {"deprecated": "true"}, "# c", "git", "/tmp/c"
    ).status == "deprecated"


# ---------- 2. set_status 往返 ----------


def test_set_status_roundtrip(conn):
    skills_dao.upsert_skill(conn, _rec("archived-one"))
    assert skills_dao.get_skill(conn, "archived-one")["status"] == "active"

    assert skills_dao.set_status(conn, "archived-one", "deprecated") is True
    assert skills_dao.get_skill(conn, "archived-one")["status"] == "deprecated"

    # 恢复
    assert skills_dao.set_status(conn, "archived-one", "active") is True
    assert skills_dao.get_skill(conn, "archived-one")["status"] == "active"


def test_set_status_rejects_unknown_status(conn):
    with pytest.raises(ValueError):
        skills_dao.set_status(conn, "x", "deleted")


# ---------- 3. 检索 / 列表默认不召回停用件 ----------


def test_fts_search_excludes_deprecated_by_default(conn):
    skills_dao.upsert_skill(conn, _rec("keywordprobe-live", content="# 关键词探针 水印"))
    skills_dao.upsert_skill(
        conn, _rec("keywordprobe-dead", content="# 关键词探针 水印", status="deprecated")
    )
    names = [h["name"] for h in skills_dao.fts_search(conn, "关键词探针 水印", limit=10)]
    assert "keywordprobe-live" in names
    assert "keywordprobe-dead" not in names

    names_all = [
        h["name"]
        for h in skills_dao.fts_search(
            conn, "关键词探针 水印", limit=10, include_deprecated=True
        )
    ]
    assert "keywordprobe-dead" in names_all


def test_list_and_count_skip_deprecated_when_asked(conn):
    skills_dao.upsert_skill(conn, _rec("list-live"))
    skills_dao.upsert_skill(conn, _rec("list-dead", status="deprecated"))

    default_names = [r["name"] for r in skills_dao.list_skills(conn)]
    assert "list-dead" in default_names  # 管理面口径保持兼容

    agent_names = [
        r["name"] for r in skills_dao.list_skills(conn, include_deprecated=False)
    ]
    assert "list-dead" not in agent_names
    assert "list-live" in agent_names
    assert skills_dao.count_skills(conn, include_deprecated=False) < skills_dao.count_skills(conn)


# ---------- 4. 硬删清库：主表 + 向量 + uses 边 ----------


def test_delete_skill_purges_all_three_tables(conn):
    skills_dao.upsert_skill(conn, _rec("purge-me", uses=["other"]))
    skills_dao.upsert_skill(conn, _rec("other"))
    skills_dao.replace_uses(conn, "purge-me", ["other"])
    conn.execute(
        "INSERT INTO skill_vectors(name, embedding, model, dims, embedded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("purge-me", b"\x00" * 8, "bge-m3", 1024, "2026-11-06T00:00:00Z"),
    )
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM skill_vectors WHERE name='purge-me'"
    ).fetchone()[0] == 1

    skills_dao.delete_skill(conn, "purge-me")
    conn.commit()

    assert skills_dao.get_skill(conn, "purge-me") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM skill_vectors WHERE name='purge-me'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM skill_uses WHERE src='purge-me' OR dst='purge-me'"
    ).fetchone()[0] == 0
    # FTS 由触发器同步：删行后不再命中
    assert [h["name"] for h in skills_dao.fts_search(conn, "purge-me")] == []


def test_deleted_skill_absent_from_search(conn):
    skills_dao.upsert_skill(conn, _rec("gone-soon", content="# 唯一词 蝾螈"))
    assert any(h["name"] == "gone-soon" for h in skills_dao.fts_search(conn, "蝾螈"))
    skills_dao.delete_skill(conn, "gone-soon")
    conn.commit()
    assert not any(h["name"] == "gone-soon" for h in skills_dao.fts_search(conn, "蝾螈"))
