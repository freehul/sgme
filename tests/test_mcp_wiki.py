"""MCP wiki 工具测试（T-22）：wiki_search / wiki_pages / wiki_page。

复用 test_mcp_server.py 的隔离模式（tmp_path 三库 + bind_app_state + server 直调）。
数据：init_wiki_fts 建 FTS（触发器自动同步）→ wiki_dao.insert_page 造两页知识文档。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import wiki_dao
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.wiki import fts as wiki_fts


def _call(mcp, name: str, args: dict):
    """同步包装 async call_tool → 返回 (text, meta)。"""
    raw = asyncio.run(mcp.call_tool(name, args))
    results, meta = raw if isinstance(raw, tuple) else (raw, None)
    text = "\n".join(c.text for c in results if getattr(c, "text", None))
    return text, meta


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    """构建绑定隔离 app_state 的 MCP server，wiki 库预置两页知识文档。"""
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    from sgme.server.app import AgentKeyStore

    _key_store = AgentKeyStore(
        admin_key="test-admin-key", agent_key="test-agent-key",
        store_path=tmp_path / "agent_keys.json",
    )
    bind_app_state({
        "cfg": cfg, "mem_conn": mem_conn,
        "session_conn": session_conn, "wiki_conn": wiki_conn,
        "key_store": _key_store,
    })
    # 先建 FTS（含存量回填），后插入（触发器自动同步索引）
    wiki_fts.init_wiki_fts(wiki_conn)
    wiki_dao.insert_page(
        wiki_conn, "p1", "SGME 架构",
        "SGME 记忆引擎采用双库架构：memory.db 记忆池与 wiki.db 场景。",
        category="design", tags=["架构"],
    )
    wiki_dao.insert_page(
        wiki_conn, "p2", "备份策略",
        "每日 04:00 自动备份到异地目录，保留最近 7 份。",
        category="ops", tags=["备份"],
    )
    server = build_mcp_server()
    yield server
    db_mod.close(mem_conn)


def test_mcp_wiki_tools_registered(mcp):
    """wiki 工具已注册（tools/list 可见）。"""
    tools = asyncio.run(mcp.list_tools())
    names = [t.name for t in tools]
    expected = {"wiki_search", "wiki_pages", "wiki_page", "wiki_page_add"}
    assert expected <= set(names), f"缺工具: {expected - set(names)}"


def test_mcp_wiki_search_hit(mcp):
    """wiki_search 命中（FTS BM25 主路或 LIKE 兜底）。"""
    text, _ = _call(mcp, "wiki_search", {"query": "备份"})
    data = json.loads(text)
    assert "results" in data
    assert any(r["page_id"] == "p2" for r in data["results"])
    assert all({"page_id", "title", "snippet"} <= set(r) for r in data["results"])


def test_mcp_wiki_search_empty(mcp):
    """wiki_search 空召回 → 空结果（不报错）。"""
    text, _ = _call(mcp, "wiki_search", {"query": "不存在的关键词xyz"})
    data = json.loads(text)
    assert data == {"results": []}


def test_mcp_wiki_pages_list(mcp):
    """wiki_pages 列表：轻量字段（不含正文/分词列）+ total。"""
    text, _ = _call(mcp, "wiki_pages", {})
    data = json.loads(text)
    assert data["total"] == 2
    assert len(data["pages"]) == 2
    for p in data["pages"]:
        assert "content" not in p
        assert "content_seg" not in p
        assert "page_id" in p and "title" in p


def test_mcp_wiki_pages_category_filter(mcp):
    """wiki_pages 按 category 过滤。"""
    text, _ = _call(mcp, "wiki_pages", {"category": "ops"})
    data = json.loads(text)
    assert data["total"] == 2  # total 为全量（对称 HTTP 端点语义）
    assert [p["page_id"] for p in data["pages"]] == ["p2"]


def test_mcp_wiki_page_detail(mcp):
    """wiki_page 详情：含正文全文，剔除分词列。"""
    text, _ = _call(mcp, "wiki_page", {"page_id": "p1"})
    data = json.loads(text)
    assert "page" in data
    assert data["page"]["page_id"] == "p1"
    assert "双库架构" in data["page"]["content"]
    assert "content_seg" not in data["page"]


def test_mcp_wiki_page_not_found(mcp):
    """wiki_page 不存在 → error 文案（MCP 扁平错误约定）。"""
    text, _ = _call(mcp, "wiki_page", {"page_id": "nope"})
    data = json.loads(text)
    assert "error" in data
    assert "不存在" in data["error"]


# ---------- wiki_page_add（直接写入，不走提炼） ----------

def test_mcp_wiki_add_create(mcp):
    """新建：page_id 自动生成 + status created + 可回读 + FTS 立即可搜（索引保证）。"""
    text, _ = _call(mcp, "wiki_page_add", {
        "title": "新知识页", "content": "渐进式披露与索引化 skill 方案",
        "category": "research", "tags": ["skill", "wiki"],
    })
    data = json.loads(text)
    assert data["status"] == "created"
    page_id = data["page_id"]
    assert page_id.startswith("新知识页")  # 标题 slug 前缀
    # 回读
    detail = json.loads(_call(mcp, "wiki_page", {"page_id": page_id})[0])
    assert detail["page"]["category"] == "research"
    assert detail["page"]["tags"] == ["skill", "wiki"]
    # FTS 可搜（create_page 内部先 init 再 insert 的索引保证）
    hits = json.loads(_call(mcp, "wiki_search", {"query": "渐进式披露"})[0])
    assert any(r["page_id"] == page_id for r in hits["results"])


def test_mcp_wiki_add_idempotent_upsert(mcp):
    """同 title+content 再写 → 同 page_id + status updated（幂等更新，不重复建页）。"""
    args = {"title": "幂等页", "content": "相同内容"}
    first = json.loads(_call(mcp, "wiki_page_add", args)[0])
    second = json.loads(_call(mcp, "wiki_page_add", args)[0])
    assert second["page_id"] == first["page_id"]
    assert second["status"] == "updated"
    lst = json.loads(_call(mcp, "wiki_pages", {})[0])
    assert lst["total"] == 3  # fixture 预置 2 页 + 本次 1 页


def test_mcp_wiki_add_missing_args(mcp):
    """缺必填参数 → 框架层 ToolError（与 wiki_page 缺参行为一致）。"""
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _call(mcp, "wiki_page_add", {"title": "只有标题"})


def test_mcp_wiki_add_blank_content(mcp):
    """空串/纯空格 → operations 业务校验（InvalidArgs → 扁平 error）。"""
    text, _ = _call(mcp, "wiki_page_add", {"title": "空正文", "content": "   "})
    data = json.loads(text)
    assert "error" in data
