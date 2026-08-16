"""tests/test_wiki_supersession.py：wiki create supersession 取代机制测试（方案 v0.3 §5.1）。

覆盖：
1. create 同 title 同 content → 命中同一 page_id，status=updated，无 superseded 旧页
2. create 同 title 不同 content → 新 page_id，旧页 status='superseded' 且 supersedes=新 page_id
3. create 同 title 不同 category → 不触发 supersession
4. create 全新 title → 无 supersession
5. supersession 后 list_pages（status='active' 过滤）只返回新页，旧页不可见
6. category=None 的同 title 旧页也触发取代（category IS NULL 分支）
"""
from __future__ import annotations

import pytest

from sgme.data import db as db_mod
from sgme.data import wiki_dao
from sgme.operations.wiki import create_page


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect_wiki(tmp_path / "data")
    yield c
    db_mod.close(c)


# ---------- create supersession ----------

def test_create_same_title_same_content_is_updated(conn):
    """同 title 同 content → 同一 page_id，upsert 更新，无 supersession。"""
    r1 = create_page(conn, title="手册", content="正文A")
    r2 = create_page(conn, title="手册", content="正文A")
    assert r1.data["page_id"] == r2.data["page_id"]
    assert r2.data["status"] == "updated"
    assert "superseded" not in r2.data
    assert wiki_dao.count_pages(conn) == 1  # 无重复建页、无旧页


def test_create_same_title_diff_content_supersedes(conn):
    """同 title 不同 content → 新 page_id，旧页 superseded 且 supersedes=新 page_id。"""
    r1 = create_page(conn, title="手册", content="正文A")
    old_id = r1.data["page_id"]
    r2 = create_page(conn, title="手册", content="正文B")
    new_id = r2.data["page_id"]
    assert new_id != old_id
    assert r2.data["status"] == "created"
    assert r2.data["superseded"] == old_id
    old = wiki_dao.get_page(conn, old_id)
    assert old["status"] == "superseded"
    assert old["supersedes"] == new_id
    new = wiki_dao.get_page(conn, new_id)
    assert new["status"] == "active"


def test_create_same_title_diff_category_no_supersession(conn):
    """同 title 不同 category → 不触发 supersession（category 不同不算同页）。"""
    r1 = create_page(conn, title="手册", content="正文A", category="cat1")
    old_id = r1.data["page_id"]
    r2 = create_page(conn, title="手册", content="正文B", category="cat2")
    assert "superseded" not in r2.data
    assert wiki_dao.get_page(conn, old_id)["status"] == "active"


def test_create_new_title_no_supersession(conn):
    """全新 title → 无 supersession。"""
    r = create_page(conn, title="全新手册", content="正文")
    assert r.data["status"] == "created"
    assert "superseded" not in r.data


def test_list_pages_hides_superseded(conn):
    """supersession 后 list_pages（status='active' 过滤）只返回新页。"""
    r1 = create_page(conn, title="手册", content="正文A")
    old_id = r1.data["page_id"]
    r2 = create_page(conn, title="手册", content="正文B")
    new_id = r2.data["page_id"]
    ids = [p["page_id"] for p in wiki_dao.list_pages(conn)]
    assert new_id in ids
    assert old_id not in ids


def test_create_same_title_none_category_supersedes(conn):
    """category=None 的同 title 旧页也触发取代（category IS NULL 分支）。"""
    r1 = create_page(conn, title="手册", content="正文A")  # category 缺省 None
    old_id = r1.data["page_id"]
    r2 = create_page(conn, title="手册", content="正文B")  # category 缺省 None
    assert r2.data["superseded"] == old_id
    assert wiki_dao.get_page(conn, old_id)["status"] == "superseded"
