"""profile/inject.py：模板查询引擎 + 注入拼装（纯 SQL，零 LLM）。

- query_section: 维度过滤(AND/any) + TTL + time_window + 默认排序
- build_inject_blocks: 拼装 blocks[] + stats
- Tier0：摘要文件不存在 → 静态维度直出降级（present:false）
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from sgme.data import memory_dao


def _relative_time(updated_at: str | None) -> str | None:
    """相对时间标注：updated_at 距今 < 30 天时返回 'N天前'。"""
    if not updated_at:
        return None
    try:
        # 解析 ISO 8601 UTC
        ut = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    now = datetime.now(timezone.utc)
    delta = now - ut
    if delta.days < 0:
        return None
    if delta.days == 0:
        secs = int(delta.total_seconds())
        if secs < 3600:
            return f"{secs // 60}分钟前" if secs >= 60 else "刚刚"
        return f"{secs // 3600}小时前"
    if delta.days < 30:
        return f"{delta.days}天前"
    return None


# ---------- 查询引擎 ----------

def query_section(
    mem_conn: sqlite3.Connection,
    section: dict,
    dimensions: list[dict],
) -> list[dict]:
    """执行单个 section 的查询（纯 SQL，零 LLM）。

    - 维度过滤：match=all → AND；match=any → OR
    - TTL 过滤：ttl_filter=true（默认）→ ttl_days IS NULL OR updated_at > now-ttl
    - time_window：updated_at > 阈值
    - priority_min：priority >= 阈值
    - 默认排序：动态维度 updated_at DESC / 静态 priority DESC
    """
    q = section.get("query", {})
    dims = q.get("dimensions", [])
    if not dims:
        return []
    match = q.get("match", "all")
    ttl_filter = q.get("ttl_filter", True)
    time_window = q.get("time_window")
    priority_min = q.get("priority_min", 0)
    sort = q.get("sort") or _default_sort(dimensions, dims)
    limit = q.get("limit", 10)

    include_expired = not ttl_filter
    time_window_start = None
    if time_window:
        from sgme.profile.template import time_window_to_threshold
        time_window_start = time_window_to_threshold(time_window)

    results = memory_dao.list_memories_by_dimension(
        mem_conn, dims, match=match, limit=limit,
        include_expired=include_expired, time_window_start=time_window_start,
        order_by=sort,
    )
    # 记录注入统计（best-effort：失败不打断注入主流程）
    from sgme.data.memory_stats_dao import record_inject
    for r in results:
        record_inject(mem_conn, r["memory_id"])
    # priority_min 过滤（memory_dao 未支持，这里过滤）
    if priority_min > 0:
        results = [r for r in results if r.get("priority", 0) >= priority_min]
    return results


def _default_sort(dimensions: list[dict], section_dims: list[str]) -> str:
    """默认排序：section 维度全为动态 → updated_at DESC；其余 → priority DESC。"""
    dim_map = {d["id"]: d for d in dimensions}
    all_dynamic = all(
        dim_map.get(d, {}).get("time_velocity") == "dynamic"
        for d in section_dims
    ) if section_dims else False
    return "updated_at DESC" if all_dynamic else "priority DESC"


# ---------- 注入拼装 ----------

def build_inject_blocks(
    template: dict,
    section_results: list[list[dict]],
    tier0_summary: str | None = None,
    avg_item_tokens: int = 30,
) -> dict:
    """拼装注入响应：{"blocks":[...], "stats":{...}}。

    - template: 加载后的模板
    - section_results: 每个 section 的查询结果（与 template.sections 顺序对应）
    - tier0_summary: Tier0 摘要文本（若 None → present:false）
    """
    blocks: list[dict] = []
    total_items = 0

    # Tier0 摘要（若有）
    if tier0_summary:
        blocks.append({
            "title": "画像摘要",
            "items": [{"content": tier0_summary}],
            "present": True,
        })
        total_items += 1

    sections = template.get("sections", [])
    # 跨 section 去重（2026-08-18 修复）：多维度记忆会在多个 section 重复命中，
    # 同一记忆只保留在首个命中的 section（memory_id 判等，注入去重不丢语义）
    seen_ids: set[str] = set()
    for section, results in zip(sections, section_results):
        items = []
        for r in results:
            mid = r.get("memory_id")
            if mid:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            content = r.get("content", "")
            rel = _relative_time(r.get("updated_at"))
            if rel:
                items.append({
                    "content": content,
                    "relative_time": rel,
                    "memory_id": r.get("memory_id"),
                })
            else:
                items.append({
                    "content": content,
                    "memory_id": r.get("memory_id"),
                })
        blocks.append({
            "title": section.get("title", ""),
            "items": items,
            "present": len(items) > 0,
        })
        total_items += len(items)

    return {
        "blocks": blocks,
        "stats": {
            "mode": template.get("name"),
            "queries": len(sections),
            "tokens_est": total_items * avg_item_tokens,
            "tier0_present": tier0_summary is not None,
        },
    }


# ---------- 完整注入 ----------

def inject(
    mem_conn: sqlite3.Connection,
    template: dict,
    dimensions: list[dict],
    tier0_summary: str | None = None,
) -> dict:
    """完整注入：遍历模板 sections → query_section → build_inject_blocks。

    Tier0 降级：tier0_summary=None → 静态维度直出（present:false 标注）。
    """
    section_results = [
        query_section(mem_conn, s, dimensions)
        for s in template.get("sections", [])
    ]
    return build_inject_blocks(template, section_results, tier0_summary=tier0_summary)
