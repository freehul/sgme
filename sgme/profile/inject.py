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
    *,
    lifecycle_active: bool = True,
    exclude_anchor: bool = True,
    exclude_valid_expired: bool = True,
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
        order_by=sort, priority_min=priority_min,
        # 2.0 D-23：needs_review=1（解析降级/缺维度）不许进主注入块与人格证据
        exclude_review=True,
        # 2.0 D-67：技能/知识候选不进画像——skill_candidate 是可复用经验（该进技能库），
        # wiki_candidate 该进知识库；1.x 存量 route 为 NULL，COALESCE 后按 memory 对待。
        routes=("memory",),
        # 2.0 D-68：遗忘请求保留可回溯但不主动提（体检/待审/检索仍可见）
        exclude_memory_types=("forget_request",),
        # 2.0 D-66：他人事实不进本人画像（存量 NULL/unknown 按本人对待）
        subject_filter=("self",),
        # 2.0 D-52/3.6（2026-11-04）：注入可见性谓词 + 锚点档剥离 + 失效断言退出
        # D-55 阶段 4（2026-11-04）：read_mode=legacy/shadow 时由调用方传 False，
        # 回退 1.x 读口径（三项全关），实现秒级切回。
        lifecycle_active=lifecycle_active,
        exclude_anchor=exclude_anchor,
        exclude_valid_expired=exclude_valid_expired,
    )
    # 记录注入统计（best-effort：失败不打断注入主流程）
    # T-203 G1：priority_min 已下推 SQL，此处 results 即最终注入集，统计不再虚高
    from sgme.data.memory_stats_dao import record_inject
    for r in results:
        record_inject(mem_conn, r["memory_id"])
    return results


def _default_sort(dimensions: list[dict], section_dims: list[str]) -> str:
    """默认排序：统一委托 ``template.default_sort``（T-203 F1）。

    此前 inject 与 template 各维护一份判定逻辑（同签名同语义），改一处漏一处
    即产生口径漂移，故收敛为单一实现。
    """
    from sgme.profile.template import default_sort
    return default_sort(dimensions, section_dims)


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
    # 同一记忆只保留在首个命中的 section（memory_id 判等，注入去重不丢语义）。
    # 内容前缀兜底：L1 提炼可能产生字符级细微差异的相似记忆（如句式微变），
    # 按 content 前 50 字判重，语义重复只注入一条（2026-08-18 升级）。
    seen_ids: set[str] = set()
    seen_prefix: set[str] = set()
    for section, results in zip(sections, section_results):
        items = []
        for r in results:
            mid = r.get("memory_id")
            content = r.get("content", "")
            if mid and mid in seen_ids:
                continue
            # 前缀归一化：截取前 50 字并去尾部标点（句号/感叹号差异视为同义）
            prefix = content[:50].rstrip("。！？!?.,，；; ")
            if prefix in seen_prefix:
                continue
            seen_prefix.add(prefix)
            if mid:
                seen_ids.add(mid)
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


# ---------- 锚点块（D-52 档 2/3）----------


ANCHOR_TITLE = "长期记忆（锚点块）"


def build_anchor_block(mem_conn: sqlite3.Connection, anchor_cfg: dict | None = None) -> dict | None:
    """构建长期锚点块（inject_tier='anchor' 的记忆不进主块，只进这里）。

    设计 §3.3.1：预算 = 注入总预算 15%（默认 2000 token 下约 300 token ≈ 3–5 条摘要），
    M 降序 → last_recalled_at 降序 → 超预算截断，并**显式标注未展开条数**
    （D-47 同款：不许静默省略）。返回 None = 无锚点记忆或开关关闭。
    """
    cfg = anchor_cfg or {}
    if not bool(cfg.get("enabled", True)):
        return None
    max_items = int(cfg.get("max_items", 5) or 5)
    where = (
        "m.status='active' AND m.inject_tier='anchor' "
        "AND (m.lifecycle IS NULL OR m.lifecycle='active') "
        "AND COALESCE(m.needs_review, 0)=0 AND COALESCE(m.route, 'memory')='memory' "
        "AND (m.subject IS NULL OR m.subject='unknown' OR lower(m.subject)='self')"
    )
    rows = mem_conn.execute(
        f"""
        SELECT m.memory_id, m.content, m.meaning_anchor, s.last_recalled_at
        FROM memories m
        LEFT JOIN memory_stats s ON s.memory_id = m.memory_id
        WHERE {where}
        ORDER BY COALESCE(m.meaning_anchor, 0) DESC,
                 COALESCE(s.last_recalled_at, '') DESC
        LIMIT ?
        """,
        (max_items,),
    ).fetchall()
    if not rows:
        return None
    total = int(
        mem_conn.execute(f"SELECT COUNT(*) FROM memories m WHERE {where}").fetchone()[0]
    )
    items: list[dict] = []
    for r in rows:
        content = str(r["content"] or "")
        summary = content if len(content) <= 80 else content[:80] + "…"
        item: dict = {"content": summary, "memory_id": r["memory_id"]}
        if r["meaning_anchor"] is not None:
            item["meaning_anchor"] = round(float(r["meaning_anchor"]), 3)
        items.append(item)
    block: dict = {
        "title": ANCHOR_TITLE,
        "kind": "anchor",
        "items": items,
        "present": True,
    }
    if total > len(items):
        # D-47 同款诚实标注：截断必须可见
        block["note"] = f"另有 {total - len(items)} 条长期记忆未展开，可按需检索"
    return block


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
    result = build_inject_blocks(template, section_results, tier0_summary=tier0_summary)
    # ST-35 T-101：性格参考块（失败不阻塞注入）
    try:
        from sgme.profile.persona_block import build_persona_block
        block = build_persona_block(mem_conn)
        if block is not None:
            result["blocks"].append(block["block"])
            result["stats"]["persona_present"] = True
            result["stats"]["tokens_est"] += block["tokens"]
        else:
            result["stats"]["persona_present"] = False
    except Exception:
        result["stats"]["persona_present"] = False
    # D-52 档 2/3：锚点块（独立预算；失败不阻塞注入）
    try:
        from sgme.config import DEFAULT_INJECT_CONFIG

        anchor_cfg = dict(DEFAULT_INJECT_CONFIG.get("anchor") or {})
    except Exception:
        anchor_cfg = {"enabled": True, "max_items": 5}
    try:
        anchor_block = build_anchor_block(mem_conn, anchor_cfg)
        if anchor_block is not None:
            result["blocks"].append(anchor_block)
            result["stats"]["anchor_present"] = True
            result["stats"]["anchor_items"] = len(anchor_block["items"])
            if anchor_block.get("note"):
                result["stats"]["anchor_note"] = anchor_block["note"]
        else:
            result["stats"]["anchor_present"] = False
    except Exception:
        result["stats"]["anchor_present"] = False
    return result
