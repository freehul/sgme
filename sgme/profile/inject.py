"""profile/inject.py：模板查询引擎 + 注入拼装（纯 SQL，零 LLM）。

- query_section: 维度过滤(AND/any) + TTL + time_window + 默认排序
- build_inject_blocks: 拼装 blocks[] + stats
- apply_inject_budget: 注入预算门控（D-49）——core > section > persona 三级裁剪
- Tier0 画像摘要已于 D-79 砍除（2026-11-05）；画像由 core 记忆卡承担
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from sgme.data import memory_dao

# 单条记忆 token 估算口径唯一真源（与模板校验 validate_template 同源，勿另立常量）
from sgme.profile.template import AVG_ITEM_TOKENS

logger = logging.getLogger("sgme.profile.inject")


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
    avg_item_tokens: int = AVG_ITEM_TOKENS,
) -> dict:
    """拼装注入响应：{"blocks":[...], "stats":{...}}。

    - template: 加载后的模板
    - section_results: 每个 section 的查询结果（与 template.sections 顺序对应）

    Tier0 摘要已于 2026-11-05（D-79）整体砍除——画像由 core 记忆卡承担，
    本函数不再渲染任何 LLM 生成的摘要块。
    """
    blocks: list[dict] = []
    total_items = 0

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
        },
    }


# ---------- 注入预算门控（D-49，2026-11-05） ----------

# 不参与 section 裁剪的附加块 kind：core 永不裁剪；persona 最先整块丢弃；
# 锚点/回指/冷场修复块沿用各自独立预算（D-49 只约束 core > section > persona 三段）。
NON_SECTION_KINDS = frozenset({
    "core_cards",        # core 记忆卡（D-79）——永不裁剪
    "persona",           # 性格参考块（D-48）——最先丢弃
    "anchor",            # 长期锚点块（D-52）——独立预算
    "last_mentions",     # 记忆回指块（D-70）——独立预算
    "coldstart_repair",  # 冷场修复提示块（D-72）——提示性块
})


def is_section_block(block: dict) -> bool:
    """模板 section 块判定：build_inject_blocks 产出的块**不带 kind**。"""
    return block.get("kind") not in NON_SECTION_KINDS


def estimate_tokens(response: dict) -> int:
    """按现网口径重算注入 token 估算（不新造公式）。

    - 模板 section 块：条数 × AVG_ITEM_TOKENS（与模板校验 validate_template 同源）
    - core 块：core_cards.build_core_block 给出的 tokens（中文 1.5 字/token + 8）
    - persona 块：persona_block.build_persona_block 给出的 tokens（每条 25）
    锚点/回指/冷场块不计入本口径（与既有 build_inject_blocks 埋点一致）。
    """
    stats = response.get("stats") or {}
    total = sum(
        len(b.get("items") or [])
        for b in response.get("blocks") or []
        if is_section_block(b)
    ) * AVG_ITEM_TOKENS
    if stats.get("core_present"):
        total += int(stats.get("core_tokens") or 0)
    if stats.get("persona_present"):
        total += int(stats.get("persona_tokens") or 0)
    return int(total)


def apply_inject_budget(response: dict, max_tokens: int | None) -> dict:
    """注入预算门控（D-49）：超限按 core > 模板 section > persona_block 裁剪。

    - max_tokens 为 None → 原样返回：不传预算的调用方行为**完全不变**。
    - core 块永不裁剪；persona 块先**整块丢弃**；仍超限再从最后一个 section 块
      尾部逐条丢 item（section 顺序即模板优先级，靠前的更保真）。
    - 裁后按现网口径重算 stats.tokens_est，过程如实写进 stats：
      budget_max_tokens / budget_tokens_before / budget_trimmed /
      persona_dropped / dropped_items / budget_unmet。
    - 极端情形（core 自身就超预算）不硬裁，置 budget_unmet=True 诚实标注。

    非正数预算按 0 处理（只保留 core）——接口契约承诺的「按预算注入」不静默失效。
    """
    if max_tokens is None:
        return response
    budget = max(0, int(max_tokens))
    stats = response.setdefault("stats", {})
    tokens_before = int(stats.get("tokens_est") or 0)
    stats["budget_max_tokens"] = budget
    stats["budget_tokens_before"] = tokens_before

    blocks = response.setdefault("blocks", [])
    dropped_items = 0
    persona_dropped = False
    if tokens_before > budget:
        # ① persona_block 先整块丢（优先级最低；不留空壳占位）
        if stats.get("persona_present"):
            for idx, block in enumerate(blocks):
                if block.get("kind") == "persona":
                    blocks.pop(idx)
                    break
            stats["persona_present"] = False
            persona_dropped = True
        # ② 模板 section 从最后一个块往前、每个块从尾部逐条丢 item
        tokens = estimate_tokens(response)
        if tokens > budget:
            for block in reversed([b for b in blocks if is_section_block(b)]):
                items = block.get("items") or []
                while items and tokens > budget:
                    items.pop()
                    tokens -= AVG_ITEM_TOKENS
                    dropped_items += 1
                block["items"] = items
                block["present"] = bool(items)  # blocks 与 items 必须一致
                if tokens <= budget:
                    break

    # 裁后重算：tokens_est 必须与最终 blocks 一致（不沿用裁剪前的旧值）
    stats["tokens_est"] = estimate_tokens(response)
    stats["budget_trimmed"] = bool(persona_dropped or dropped_items)
    stats["persona_dropped"] = persona_dropped
    stats["dropped_items"] = dropped_items
    stats["budget_unmet"] = stats["tokens_est"] > budget
    # D-47 同款纪律：裁剪不许静默省略——note 写明裁了什么、去哪里取全文
    if stats["budget_trimmed"] or stats["budget_unmet"]:
        parts = [
            f"注入预算裁剪：max_tokens={budget}，",
            f"tokens {tokens_before}→{stats['tokens_est']}，",
            f"丢弃 section 条目 {dropped_items} 条",
        ]
        if persona_dropped:
            parts.append("，persona 块已整块丢弃")
        if stats["budget_unmet"]:
            parts.append("；core 块超出预算未裁（budget_unmet）")
        parts.append("；被裁内容可按需经 /v1/search 检索全文")
        current = str(stats.get("note") or "").strip()
        stats["note"] = (current + "\n" + "".join(parts)).strip()
    return response


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
) -> dict:
    """完整注入：遍历模板 sections → query_section → build_inject_blocks。

    Tier0 已砍除（D-79，2026-11-05）；画像由 core 记忆卡承担。
    """
    section_results = [
        query_section(mem_conn, s, dimensions)
        for s in template.get("sections", [])
    ]
    result = build_inject_blocks(template, section_results)
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
