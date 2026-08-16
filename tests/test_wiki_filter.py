"""tests/test_wiki_filter.py：W2 检索语义测试（方案 v0.3 §5.2）。

覆盖：
1. search_wiki_fts 过滤 superseded（BM25 路径 + LIKE 兜底路径两处）
2. _search_wiki_pages（统一搜索 wiki_pages 层）默认排除 skill 标记页
3. list_pages 过滤 superseded
4. search_wiki_fts 返回结果带 tags（供上层 skill 过滤）
"""
from __future__ import annotations

import pytest

from sgme.data import db as db_mod
from sgme.data import wiki_dao
from sgme.operations.search import _search_wiki_pages
from sgme.wiki import fts as wiki_fts_mod


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect_wiki(tmp_path / "data")
    yield c
    db_mod.close(c)


def _init_fts(conn):
    assert wiki_fts_mod.init_wiki_fts(conn) is True


# ---------- superseded 过滤（fts 层，两路径） ----------

def test_fts_filters_superseded_bm25(conn):
    """BM25 路径：superseded 页内容命中但不返回（status='active' 过滤）。"""
    wiki_dao.insert_page(conn, "active1", "部署", "xray 部署教程")
    wiki_dao.insert_page(conn, "old1", "部署", "旧版部署教程", status="superseded")
    _init_fts(conn)
    results = wiki_fts_mod.search_wiki_fts(conn, "部署", limit=5)
    ids = {r["page_id"] for r in results}
    assert "active1" in ids
    assert "old1" not in ids


def test_fts_filters_superseded_like(conn):
    """LIKE 兜底路径：FTS 空召回走 LIKE 时同样过滤 superseded。"""
    wiki_dao.insert_page(conn, "a1", "t", "独特词xyz 内容")
    wiki_dao.insert_page(conn, "o1", "t", "独特词xyz 旧内容", status="superseded")
    _init_fts(conn)
    results = wiki_fts_mod.search_wiki_fts(conn, "独特词xyz", limit=5)
    ids = {r["page_id"] for r in results}
    assert "a1" in ids
    assert "o1" not in ids


def test_fts_results_carry_tags(conn):
    """search_wiki_fts 返回结果带 tags 字段（供统一搜索 Python 层 skill 过滤）。"""
    wiki_dao.insert_page(conn, "p1", "手册", "正文", tags=["skill", "sgme"])
    _init_fts(conn)
    results = wiki_fts_mod.search_wiki_fts(conn, "手册", limit=5)
    row = next(r for r in results if r["page_id"] == "p1")
    assert "skill" in row["tags"]


# ---------- skill 过滤（统一搜索入口 Python 层） ----------

def test_search_wiki_pages_excludes_skill(conn):
    """统一搜索 wiki_pages 层默认排除 skill 标记页（回忆通道不见手册）。"""
    wiki_dao.insert_page(conn, "handbook1", "SGME操作手册", "运维手册正文",
                         category="skill/sgme", tags=["skill", "sgme"])
    wiki_dao.insert_page(conn, "note1", "研究笔记", "手册相关研究内容",
                         category="design", tags=["research"])
    _init_fts(conn)
    results = _search_wiki_pages(conn, "手册", 10)
    ids = {r["page_id"] for r in results}
    assert "note1" in ids or "handbook1" in ids  # 至少一个命中
    assert "handbook1" not in ids  # skill 页被排除


def test_search_wiki_pages_keeps_normal(conn):
    """非 skill 标记页在统一搜索中正常返回。"""
    wiki_dao.insert_page(conn, "note1", "研究笔记", "独特检索词abc 研究内容",
                         category="design", tags=["research"])
    _init_fts(conn)
    results = _search_wiki_pages(conn, "独特检索词abc", 10)
    assert any(r["page_id"] == "note1" for r in results)


# ---------- list_pages 过滤 ----------

def test_list_pages_filters_superseded(conn):
    """列表（category 浏览）不返回 superseded 页。"""
    wiki_dao.insert_page(conn, "a1", "当前手册", "x", category="skill/sgme")
    wiki_dao.insert_page(conn, "o1", "旧手册", "x", category="skill/sgme",
                         status="superseded")
    pages = wiki_dao.list_pages(conn, category="skill/sgme")
    ids = {p["page_id"] for p in pages}
    assert "a1" in ids
    assert "o1" not in ids
