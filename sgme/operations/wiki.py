"""operations/wiki.py：wiki 页面浏览/检索操作（T-22，MCP wiki 工具后端）。

三段式结构（照抄 memory.py 样板）：操作函数返回 ``OperationResult`` 信息超集，
入口层（MCP）只做协议翻译。

承接的入口
----------
========================================  ===================================
入口                                       操作
========================================  ===================================
MCP  ``wiki_search``                       ``search``（无投影，data 即响应）
MCP  ``wiki_pages``                        ``list_pages``（无投影，data 即响应）
MCP  ``wiki_page``                         ``get_page``（无投影，data 即响应）
========================================  ===================================

数据源边界（重要，2026-08-13 查证）：wiki 扩展的知识页面（``wiki_pages`` 表，
经 ingest 提炼入库）与记忆引擎的 L2 场景（``scenes`` 表）是**两个不同数据源**——
本模块操作 wiki_pages（对应 HTTP ``/v1/wiki/*``）；L2 场景检索走
``operations.search`` 的 wiki 层（scopes=["wiki"]），互不混淆。

说明：wiki 扩展模块的 HTTP 路由（``wiki/routes.py``）历史直连 ``wiki_dao``/
``wiki_fts``，未迁移到本层（扩展模块按 config 开关挂载，核心零影响优先）；
本模块是 MCP 通道的 operations 化入口。
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from sgme.data import wiki_dao
from sgme.operations.errors import ERR_INTERNAL, ERR_NOT_FOUND, InvalidArgs, OperationResult
from sgme.wiki import fts as wiki_fts

# 列表投影剔除的大字段（content 全文 / content_seg 分词列），避免响应臃肿
_LIST_SKIP_FIELDS = ("content", "content_seg", "description_seg")


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
) -> OperationResult:
    """wiki 知识库检索（wiki_fts BM25 + LIKE 兜底，对称 HTTP /v1/wiki/search）。

    返回 [{page_id, title, snippet}]（snippet 为 content 前 200 字符）。
    """
    if not query or not query.strip():
        return OperationResult.succeed({"results": []})
    results = wiki_fts.search_wiki_fts(conn, query, limit=limit)
    return OperationResult.succeed({"results": results})


def list_pages(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> OperationResult:
    """wiki 页面列表（updated_at 降序；category 可选过滤）。

    返回轻量字段（page_id/title/category/tags/source_*/ingested_at/updated_at），
    不含正文——正文走 ``get_page`` 按需取。
    """
    pages = wiki_dao.list_pages(conn, category=category, limit=limit, offset=offset)
    light = [
        {k: v for k, v in p.items() if k not in _LIST_SKIP_FIELDS}
        for p in pages
    ]
    return OperationResult.succeed(
        {"pages": light, "total": wiki_dao.count_pages(conn)}
    )


def get_page(conn: sqlite3.Connection, page_id: str) -> OperationResult:
    """wiki 页面详情（含 content 正文全文，剔除 content_seg 分词列）。"""
    page = wiki_dao.get_page(conn, page_id)
    if page is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"页面不存在: {page_id}")
    page.pop("content_seg", None)
    return OperationResult.succeed({"page": page})


def update_page(
    conn: sqlite3.Connection,
    page_id: str,
    content: str,
    append: bool = True,
    title: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
    author: str | None = None,
) -> OperationResult:
    """按 page_id 更新/追加 wiki 页面（自进化写回主通道，W3 方案 v0.3 §5.3）。

    append=True（默认）：content 追加到现有正文末尾（ADD-only），追加片段自带
    「来源 + entry hash」标记；入口查重（现有 content 检索 hash，已存在 → noop，
    幂等）。append=False：整体替换 content。
    description 默认不动（显式传才更新——追加经验不改页级摘要，P2 修订）。
    """
    page = wiki_dao.get_page(conn, page_id)
    if page is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"页面不存在: {page_id}")
    entry_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
    marker = f"hash: {entry_hash}"
    if append:
        if marker in (page.get("content") or ""):
            return OperationResult.succeed(
                {"page_id": page_id, "status": "noop", "reason": "entry hash 已存在，幂等跳过"}
            )
        source = f"来源: {author or 'agent'}"
        new_content = (page.get("content") or "").rstrip() + f"\n\n> {source} | {marker}\n{content}"
    else:
        new_content = content
    ok = wiki_dao.update_page_content(
        conn, page_id, new_content,
        title=title, category=category, tags=tags, description=description,
    )
    if not ok:
        return OperationResult.fail(ERR_INTERNAL, "页面更新失败")
    return OperationResult.succeed(
        {"page_id": page_id, "status": "appended" if append else "updated"}
    )


def create_page(
    conn: sqlite3.Connection,
    *,
    title: str,
    content: str,
    category: str | None = None,
    tags: list[str] | None = None,
    source_type: str = "text",
    source_url: str | None = None,
    source_file: str | None = None,
    description: str | None = None,
    author: str | None = None,
    status: str | None = None,
    supersedes: str | None = None,
) -> OperationResult:
    """wiki 页面直接写入（原样入库，不走 LLM 提炼；T-55）。

    幂等语义：page_id 由「标题 slug + 内容哈希」决定——同 title+content 重复
    写入命中同一 page_id → upsert 更新（status=updated），不会重复建页。

    supersession（方案 v0.3 §5.1）：同 title 但 content 不同（生成新 page_id）且
    存在「同 category + 同 title」的 active 旧页时，旧页置 status='superseded' +
    supersedes=新 page_id，返回 data 额外带 superseded 字段（旧 page_id）。

    索引保证：先 ``init_wiki_fts``（幂等，确保虚拟表与触发器就位）再
    ``insert_page``（FTS 触发器自动同步）——冷启动库（FTS 未初始化）写入后
    也立即可被 wiki_search 检索，不存在「有页面无索引」状态。
    """
    if not title or not title.strip():
        raise InvalidArgs("title 不能为空")
    if not content or not content.strip():
        raise InvalidArgs("content 不能为空")

    from sgme.refinery.output import _gen_page_id  # 与提炼链路 page_id 格式一致

    page_id = _gen_page_id(title, content)
    exists = wiki_dao.get_page(conn, page_id) is not None
    # supersession（方案 v0.3 §5.1）：存在「同 category + 同 title」的 active 旧页且
    # content 不同（page_id 由 title+content 哈希决定，content 不同 ⇒ page_id 不同）→
    # 新页为新版本（新 page_id），旧页置 status='superseded' + supersedes=新 page_id。
    superseded = None
    if not exists:
        old_page = wiki_dao.find_active_same_title(conn, title, category)
        if old_page is not None and old_page["page_id"] != page_id:
            wiki_dao.mark_superseded(conn, old_page["page_id"], page_id)
            superseded = old_page["page_id"]
    wiki_fts.init_wiki_fts(conn)  # 幂等：触发器先就位，插入即索引
    wiki_dao.insert_page(
        conn, page_id,
        title=title, content=content,
        category=category, tags=tags,
        source_type=source_type, source_url=source_url, source_file=source_file,
        description=description, author=author, status=status, supersedes=supersedes,
    )
    result = {"page_id": page_id, "status": "updated" if exists else "created"}
    if superseded is not None:
        result["superseded"] = superseded
    return OperationResult.succeed(result)

