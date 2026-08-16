"""tests/test_wiki_columns.py：W1 迁移与字段测试（wiki_pages 新列）。

覆盖（对应方案 v0.3 §5.1 / §6 W1 验收）：
1. 老库缺列 → _migrate_wiki_page_columns 自动补列（幂等 + status 默认 active）
2. insert_page 带 description → 回读 + description_seg 生成
3. FTS 命中 description（description_seg 进索引；LIKE 兜底不查 description，
   本用例若 FTS 未建好必然红——精确暴露问题）
4. 老结构 wiki_fts（缺 description_seg）→ init_wiki_fts 自动重建
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import wiki_dao
from sgme.wiki import fts as wiki_fts_mod

# 老库结构（无 description/description_seg/author/status/supersedes）
OLD_WIKI_PAGES_DDL = """
CREATE TABLE wiki_pages (
  page_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  category TEXT,
  tags TEXT,
  source_type TEXT,
  source_url TEXT,
  source_file TEXT,
  ingested_at TEXT,
  updated_at TEXT,
  content_seg TEXT);
"""

# 老结构 FTS（只索引 content_seg）
OLD_WIKI_FTS_DDL = """
CREATE VIRTUAL TABLE wiki_fts USING fts5(
    content_seg, page_id UNINDEXED,
    content='wiki_pages', content_rowid='rowid'
);
"""


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect_wiki(tmp_path / "data")
    yield c
    db_mod.close(c)


# ---------- 迁移 ----------

def test_migrate_adds_columns_to_old_db():
    """老库（无新列）→ 迁移补 5 列 + status 默认 active（幂等可重跑）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(OLD_WIKI_PAGES_DDL)
    db_mod._migrate_wiki_page_columns(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()]
    for c in ("description", "description_seg", "author", "status", "supersedes"):
        assert c in cols, f"迁移后缺列: {c}"
    # 幂等：重跑不报错
    db_mod._migrate_wiki_page_columns(conn)
    # 老数据 status 默认 active
    conn.execute(
        "INSERT INTO wiki_pages (page_id, title, content) VALUES ('old', 't', 'c')"
    )
    conn.commit()
    row = conn.execute("SELECT status FROM wiki_pages WHERE page_id='old'").fetchone()
    assert row["status"] == "active"


def test_migrate_noop_on_missing_table():
    """wiki_pages 表不存在时迁移函数 no-op（不炸，配合 _ensure_schema 顺序）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_mod._migrate_wiki_page_columns(conn)  # 应静默返回


# ---------- DAO ----------

def test_insert_page_with_description(conn):
    wiki_dao.insert_page(
        conn, "p1", "手册", "正文内容", description="渐进式披露操作手册"
    )
    page = wiki_dao.get_page(conn, "p1")
    assert page["description"] == "渐进式披露操作手册"
    assert page["description_seg"]  # jieba 分词已生成
    assert page["status"] == "active"


def test_update_page_content_description(conn):
    """update 更新 description 时 description_seg 同步重算；不传则不动。"""
    wiki_dao.insert_page(conn, "p1", "手册", "正文", description="旧描述")
    wiki_dao.update_page_content(conn, "p1", "新正文", description="新描述词xyz")
    page = wiki_dao.get_page(conn, "p1")
    assert page["description"] == "新描述词xyz"
    assert page["description_seg"]
    assert page["content"] == "新正文"
    # 不传 description → 保持
    wiki_dao.update_page_content(conn, "p1", "再改正文")
    assert wiki_dao.get_page(conn, "p1")["description"] == "新描述词xyz"


# ---------- FTS ----------

def test_fts_hits_description(conn):
    """description 中的独特词可被 FTS 命中（description_seg 进索引）。

    LIKE 兜底只查 content/title，若 description_seg 未进 FTS 本用例必红。
    """
    wiki_dao.insert_page(
        conn, "p1", "手册", "正文不含该词", description="独特词xyz 专有描述"
    )
    assert wiki_fts_mod.init_wiki_fts(conn) is True
    results = wiki_fts_mod.search_wiki_fts(conn, "独特词xyz", limit=5)
    assert any(r["page_id"] == "p1" for r in results)


def test_fts_rebuild_adds_description_seg(conn):
    """老结构 wiki_fts（缺 description_seg）→ init_wiki_fts 自动重建后命中。"""
    conn.execute("DROP TABLE IF EXISTS wiki_fts")
    conn.execute(OLD_WIKI_FTS_DDL)
    conn.commit()
    wiki_dao.insert_page(
        conn, "p1", "手册", "正文", description="重建测试词xyz"
    )
    assert wiki_fts_mod.init_wiki_fts(conn) is True
    results = wiki_fts_mod.search_wiki_fts(conn, "重建测试词xyz", limit=5)
    assert any(r["page_id"] == "p1" for r in results)

def test_fts_rebuild_when_trigger_stale(conn):
    """新结构 FTS 表 + 旧版触发器残留（DROP TABLE 不删触发器，真实升级路径）
    → init_wiki_fts 检测触发器缺 description_seg 一并重建（2026-08-16 修复）。"""
    conn.execute("DROP TABLE IF EXISTS wiki_fts")
    conn.execute("DROP TRIGGER IF EXISTS wiki_ai")
    conn.execute("DROP TRIGGER IF EXISTS wiki_ad")
    conn.execute("DROP TRIGGER IF EXISTS wiki_au")
    # 新结构表 + 旧版触发器（只同步 content_seg）——模拟真实库升级后残留
    conn.execute(
        "CREATE VIRTUAL TABLE wiki_fts USING fts5("
        " content_seg, description_seg, page_id UNINDEXED,"
        " content='wiki_pages', content_rowid='rowid')"
    )
    conn.executescript(
        "CREATE TRIGGER wiki_ai AFTER INSERT ON wiki_pages BEGIN"
        "  INSERT INTO wiki_fts(rowid, content_seg, page_id)"
        "  VALUES (new.rowid, new.content_seg, new.page_id);"
        "END;"
    )
    conn.commit()
    assert wiki_fts_mod._triggers_have_description(conn) is False
    assert wiki_fts_mod.init_wiki_fts(conn) is True
    assert wiki_fts_mod._triggers_have_description(conn) is True
    # 重建后带 description 的新页可被 FTS 命中
    wiki_dao.insert_page(conn, "p1", "手册", "正文", description="触发器重建词xyz")
    results = wiki_fts_mod.search_wiki_fts(conn, "触发器重建词xyz", limit=5)
    assert any(r["page_id"] == "p1" for r in results)
