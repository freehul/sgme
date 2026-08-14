"""tests/test_wiki.py：wiki 扩展模块测试（v0.7 §10）。

覆盖：
1. wiki_dao CRUD（insert/get/list/update/delete + tags JSON + 幂等 upsert）
2. wiki_fts 初始化 + BM25 检索 + LIKE 兜底
3. 路由冒烟（列表/详情/HTML 渲染/导出/搜索/404）
4. 渲染函数（markdown 简单渲染 + 转义安全）
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme.data import db as db_mod
from sgme.data import wiki_dao
from sgme.wiki import fts as wiki_fts_mod
from sgme.wiki.routes import build_page_html, render_markdown_simple
from sgme.server.app import create_app


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect_wiki(tmp_path / "data")
    yield c
    db_mod.close(c)


@pytest.fixture
def app(tmp_path):
    mem = db_mod.connect_memory(tmp_path / "data")
    session = db_mod.connect_session(tmp_path / "data")
    wiki = db_mod.connect_wiki(tmp_path / "data")
    cfg = {"paths": {"data_dir": str(tmp_path / "data")}, "dimensions": {}, "aliases": {}}
    application = create_app(
        cfg=cfg, mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin", agent_key="test-agent",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
    )
    yield application
    db_mod.close(mem)
    db_mod.close(session)
    db_mod.close(wiki)


@pytest.fixture
def client(app):
    return TestClient(app)


AGENT = {"X-API-Key": "test-agent"}


# ---------- DAO ----------

def test_insert_get_page(conn):
    wiki_dao.insert_page(conn, "p1", "测试页", "正文内容", category="tech",
                         tags=["AI", "记忆"], source_type="file", source_file="raw/x.md")
    page = wiki_dao.get_page(conn, "p1")
    assert page["title"] == "测试页"
    assert page["tags"] == ["AI", "记忆"]
    assert page["category"] == "tech"
    assert page["content_seg"]  # 分词已生成


def test_parse_tags_double_encoded(conn):
    """双重 JSON 编码的 tags（历史脏数据）应被解析为列表，而非逐字符 str。"""
    # 直接写入双重编码的原始值（模拟历史坏数据）
    conn.execute(
        "INSERT INTO wiki_pages (page_id,title,content,tags,ingested_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("p_double", "测试", "正文",
         json.dumps(json.dumps(["AI", "记忆"], ensure_ascii=False), ensure_ascii=False),
         "2026-08-14T00:00:00Z", "2026-08-14T00:00:00Z"),
    )
    conn.commit()
    page = wiki_dao.get_page(conn, "p_double")
    assert page["tags"] == ["AI", "记忆"]
    # 非法 JSON 兜底为空列表
    conn.execute("UPDATE wiki_pages SET tags=? WHERE page_id=?", ("not-json", "p_double"))
    conn.commit()
    assert wiki_dao.get_page(conn, "p_double")["tags"] == []


def test_insert_idempotent_upsert(conn):
    wiki_dao.insert_page(conn, "p1", "标题A", "内容A")
    wiki_dao.insert_page(conn, "p1", "标题B", "内容B")
    page = wiki_dao.get_page(conn, "p1")
    assert page["title"] == "标题B"
    assert page["content"] == "内容B"
    assert wiki_dao.count_pages(conn) == 1


def test_list_filter_category(conn):
    wiki_dao.insert_page(conn, "p1", "一", "x", category="tech")
    wiki_dao.insert_page(conn, "p2", "二", "y", category="life")
    pages = wiki_dao.list_pages(conn, category="tech")
    assert [p["page_id"] for p in pages] == ["p1"]


def test_update_and_delete(conn):
    wiki_dao.insert_page(conn, "p1", "一", "x")
    assert wiki_dao.update_page_content(conn, "p1", "新内容", title="新标题") is True
    assert wiki_dao.update_page_content(conn, "不存在", "x") is False
    page = wiki_dao.get_page(conn, "p1")
    assert page["title"] == "新标题" and page["content"] == "新内容"
    wiki_dao.insert_link(conn, "p1", "p2", rel_type="similar")
    assert len(wiki_dao.list_links(conn, "p1")) == 1
    assert wiki_dao.delete_page(conn, "p1") is True
    assert wiki_dao.get_page(conn, "p1") is None
    assert wiki_dao.delete_page(conn, "p1") is False


# ---------- FTS ----------

def test_wiki_fts_init_and_search(conn):
    wiki_dao.insert_page(conn, "p1", "VPS 部署", "xray VLESS 部署教程", tags=["VPS"])
    wiki_dao.insert_page(conn, "p2", "抖音", "封面设计规范", tags=["抖音"])
    assert wiki_fts_mod.init_wiki_fts(conn) is True
    results = wiki_fts_mod.search_wiki_fts(conn, "部署", limit=5)
    assert any(r["page_id"] == "p1" for r in results)
    # 触发器同步：更新后仍可检索
    wiki_dao.update_page_content(conn, "p2", "抖音运营与封面")
    results = wiki_fts_mod.search_wiki_fts(conn, "运营", limit=5)
    assert any(r["page_id"] == "p2" for r in results)


def test_wiki_fts_like_fallback(conn):
    wiki_dao.insert_page(conn, "p1", "标题含独特词xyz", "内容含独特词xyz")
    results = wiki_fts_mod.search_wiki_fts(conn, "独特词xyz", limit=5)
    assert any(r["page_id"] == "p1" for r in results)


# ---------- 渲染 ----------

def test_render_markdown_simple():
    html_text = render_markdown_simple("# 标题\n\n正文段落\n\n```python\nprint('x')\n```")
    assert "<h1>标题</h1>" in html_text
    assert "<pre>" in html_text
    assert "&#x27;" in html_text  # 代码内单引号被转义


def test_render_escapes_html():
    html_text = render_markdown_simple("<script>alert(1)</script>")
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text


def test_build_page_html():
    page = {"page_id": "p1", "title": "测试", "content": "# 你好", "category": "tech",
            "tags": ["a", "b"], "updated_at": "2026-01-01T00:00:00Z"}
    doc = build_page_html(page)
    assert "<h1>测试</h1>" in doc
    assert "<html" in doc and "<head>" in doc


# ---------- 路由 ----------

def test_wiki_routes_flow(client):
    # 无页面 → 空列表
    r = client.get("/v1/wiki/pages", headers=AGENT)
    assert r.status_code == 200
    assert r.json()["total"] == 0
    # 404
    assert client.get("/v1/wiki/pages/nope", headers=AGENT).status_code == 404
    # 插入页面（直连 DAO）
    conn = client.app.state.wiki_conn
    wiki_dao.insert_page(conn, "p1", "测试页", "# 标题\n正文", category="tech", tags=["AI"])
    wiki_fts_mod.init_wiki_fts(conn)
    # 列表
    r = client.get("/v1/wiki/pages", headers=AGENT)
    assert r.json()["total"] == 1
    # 详情 JSON
    r = client.get("/v1/wiki/pages/p1", headers=AGENT)
    assert r.json()["title"] == "测试页"
    # HTML 视图
    r = client.get("/v1/wiki/pages/p1?view=html", headers=AGENT)
    assert r.status_code == 200 and "<h1>测试页</h1>" in r.text
    # 导出
    r = client.get("/v1/wiki/pages/p1/export", headers=AGENT)
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    # 搜索
    r = client.get("/v1/wiki/search", params={"q": "正文"}, headers=AGENT)
    assert r.json()["results"][0]["page_id"] == "p1"
    # 空查询
    r = client.get("/v1/wiki/search", params={"q": ""}, headers=AGENT)
    assert r.json()["results"] == []
    # 无鉴权 → 401/403
    r = client.get("/v1/wiki/pages")
    assert r.status_code in (401, 403)


# ---------- ingest（mock refinery） ----------

def test_ingest_flow_done(client, monkeypatch):
    """mock refinery.refine 成功 → 任务 done + 页面可查。"""
    import importlib

    refinery_mod = importlib.import_module("sgme.refinery")

    class FakeResult:
        ok = True
        error = None
        source_type = "text"
        title = "提炼页"
        content = "# 提炼内容"
        category = "tech"
        tags = ["AI"]
        ingested_at = "2026-08-08T00:00:00Z"

    def fake_refine(source, *a, **kw):
        assert source == "测试文本材料"
        return FakeResult()

    monkeypatch.setattr(refinery_mod, "refine", fake_refine)

    r = client.post("/v1/wiki/ingest", json={
        "source_type": "text", "content": "测试文本材料", "category": "tech",
    }, headers=AGENT)
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    # 等待后台线程完成
    import time
    for _ in range(50):
        st = client.get(f"/v1/wiki/ingest/{task_id}", headers=AGENT).json()
        if st["status"] != "queued":
            break
        time.sleep(0.05)
    assert st["status"] == "done"
    assert st["page_id"]
    # 页面已入库且可检索
    page = client.get(f"/v1/wiki/pages/{st['page_id']}", headers=AGENT).json()
    assert page["content"] == "# 提炼内容"


def test_ingest_flow_error(client, monkeypatch):
    """mock refinery.refine 失败 → 任务 error 且不落页。"""
    import importlib

    refinery_mod = importlib.import_module("sgme.refinery")

    class FakeFail:
        ok = False
        error = "LLM 提取失败"
        source_type = "text"

    monkeypatch.setattr(refinery_mod, "refine", lambda source, *a, **kw: FakeFail())

    r = client.post("/v1/wiki/ingest", json={
        "source_type": "text", "content": "坏材料",
    }, headers=AGENT)
    task_id = r.json()["task_id"]
    import time
    for _ in range(50):
        st = client.get(f"/v1/wiki/ingest/{task_id}", headers=AGENT).json()
        if st["status"] != "queued":
            break
        time.sleep(0.05)
    assert st["status"] == "error"
    assert "LLM 提取失败" in st["error"]


def test_ingest_invalid_args(client):
    r = client.post("/v1/wiki/ingest", json={"source_type": "video"}, headers=AGENT)
    assert r.status_code == 400
    r = client.post("/v1/wiki/ingest", json={"source_type": "text"}, headers=AGENT)
    assert r.status_code == 400
    # 任务不存在
    r = client.get("/v1/wiki/ingest/nonexistent", headers=AGENT)
    assert r.status_code == 404


# ---------- 直接写入（POST /v1/wiki/pages，不走提炼通道） ----------

def test_wiki_pages_create_and_searchable(client):
    """直接建页：created + 可回读 + FTS 立即可搜（冷启动库也保证索引）。"""
    r = client.post("/v1/wiki/pages", json={
        "title": "直写页", "content": "直接写入的正文内容", "category": "tech",
        "tags": ["直写"], "source_type": "text",
    }, headers=AGENT)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "created"
    page_id = body["page_id"]
    assert page_id.startswith("直写页")
    # 回读
    page = client.get(f"/v1/wiki/pages/{page_id}", headers=AGENT).json()
    assert page["content"] == "直接写入的正文内容"
    assert page["tags"] == ["直写"]
    # FTS 立即可搜（create_page 内部先 init 再 insert 的索引保证）
    hits = client.get("/v1/wiki/search", params={"q": "直写"}, headers=AGENT).json()
    assert any(h["page_id"] == page_id for h in hits["results"])


def test_wiki_pages_create_idempotent(client):
    """重复 POST 同内容 → 同 page_id + status updated（幂等更新，不重复建页）。"""
    payload = {"title": "幂等", "content": "一样的内容"}
    first = client.post("/v1/wiki/pages", json=payload, headers=AGENT).json()
    second = client.post("/v1/wiki/pages", json=payload, headers=AGENT).json()
    assert second["page_id"] == first["page_id"]
    assert second["status"] == "updated"


def test_wiki_pages_create_invalid(client):
    """缺必填参数 → 422（FastAPI/Pydantic 标准语义）。"""
    r = client.post("/v1/wiki/pages", json={"title": "只有标题"}, headers=AGENT)
    assert r.status_code == 422
    r = client.post("/v1/wiki/pages", json={"content": "只有正文"}, headers=AGENT)
    assert r.status_code == 422


def test_wiki_pages_create_unauthorized(client):
    """未鉴权 → 401/403。"""
    r = client.post("/v1/wiki/pages", json={"title": "x", "content": "y"})
    assert r.status_code in (401, 403)
