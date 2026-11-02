"""refinery/output.py：统一产出格式。

- RefineryResult：管线最终结果（ok/source_type/title/content/tags/category/error）
- to_wiki_page()：RefineryResult → wiki_pages 表行 dict（§8.4 表结构）
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class RefineryResult:
    """知识提炼管线统一产出。

    - ok: 是否成功（ingest/extract/validate 任一失败为 False）
    - source_type: text / file / url / image / video
    - title: 提炼出的页面标题
    - content: 提炼出的正文（Markdown）
    - tags: 标签列表
    - category: 分类
    - error: 失败原因（成功时为 None）
    """

    ok: bool
    source_type: str
    title: str | None = None
    content: str | None = None
    tags: list[str] = field(default_factory=list)
    category: str | None = None
    error: str | None = None

    @classmethod
    def failure(cls, source_type: str, error: str) -> "RefineryResult":
        """构造失败结果。"""
        return cls(ok=False, source_type=source_type, error=error)


def _gen_page_id(title: str | None, content: str) -> str:
    """生成 wiki_pages.page_id：标题 slug + 内容短哈希（防重名冲突）。"""
    base = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (title or "untitled").strip().lower())
    base = base.strip("-")[:64] or "page"
    digest = hashlib.sha256(f"{title}|{content}".encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_wiki_page(
    result: RefineryResult,
    page_id: str | None = None,
    source_url: str | None = None,
    source_file: str | None = None,
    ingested_at: str | None = None,
) -> dict[str, Any]:
    """RefineryResult → wiki_pages 表行 dict（§8.4）。

    字段对齐 wiki_pages 表：
    page_id / title / content / category / tags(JSON 数组) / source_type /
    source_url / source_file / ingested_at / updated_at / content_seg(None)

    Args:
        result: 成功的 RefineryResult（ok=False 时抛 ValueError）。
        page_id: 自定义页面 ID；缺省自动生成（标题 slug + 内容哈希）。
        source_url / source_file: 原始来源（来自 ingest 元数据）。
        ingested_at: 入库时间；缺省当前 UTC 时间。

    Returns:
        wiki_pages 行 dict。

    Raises:
        ValueError: result.ok 为 False 时拒绝产出。
    """
    if not result.ok:
        raise ValueError(f"失败结果不能产出 wiki 页: {result.error}")
    if not result.content:
        raise ValueError("RefineryResult.content 为空，无法产出 wiki 页")

    now = ingested_at or _now_iso()
    return {
        "page_id": page_id or _gen_page_id(result.title, result.content),
        "title": result.title or "",
        "content": result.content,
        "category": result.category,
        "tags": json.dumps(result.tags, ensure_ascii=False),
        "source_type": result.source_type,
        "source_url": source_url,
        "source_file": source_file,
        "ingested_at": now,
        "updated_at": now,
        "content_seg": None,  # jieba 分词由 wiki 模块入库时生成
    }
