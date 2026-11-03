# -*- coding: utf-8 -*-
"""profile/last_mentions.py：记忆回指块（D-70）。

**解决什么**：画像注入只给「你是谁」，用户感知不到「你记得我刚才说的事」。
回指块在画像块末尾放 1–2 条「上次提到的事」，让消费方 agent 能主动接住话题。
竞品普遍缺这一块，而这是用户唯一能直接感知的记忆价值（Replika 可查看记忆、
Kindroid 大脑图标都是同一诉求的侧面实现）。

**取数规则（D-70 定稿，逐条对应）**：
1. 候选 = 最近 ``days`` 天 ``updated_at`` 的 focus / status 维度记忆（关系维度
   按 3.0.1 不做，不得进入取数条件）；
2. 排除遗忘请求（D-68）与他人事实（D-66，``subject`` 非 self；NULL/unknown 视为本人）；
3. 排除本会话已出现过的（防复读）——由调用方传 ``exclude_contents``；
4. 按维度权重 + recency 排序取前 ``limit`` 条（focus 优先于 status，同级按时间）；
5. 预算 60–100 token/轮，超预算截断到 1 条；
6. 开关 ``inject.last_mentions.enabled``（**默认 off**，用户 10-30 拍板首月关闭）。

零 LLM、纯 SQL，符合「注入零物化」铁律。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger("sgme.profile.last_mentions")

# 回指候选维度（D-70 第 1 条：只有这两个，不得扩展）
CANDIDATE_DIMENSIONS: tuple[str, ...] = ("focus", "status")

# 默认预算（字符）：60–100 token ≈ 40–70 汉字；1 条约 15–30 字，故 2 条约 60 字
DEFAULT_CHAR_BUDGET = 70


def pick(
    conn: sqlite3.Connection,
    *,
    limit: int = 2,
    days: int = 7,
    exclude_contents: set[str] | None = None,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """挑出可回指的「上次提到的事」（按 D-70 六条规则）。

    Returns:
        ``[{"memory_id", "content", "dimension", "updated_at"}, ...]``，按优先级排序；
        无候选返回空列表。任何异常返回空列表（回指是加分项，不能拖垮注入）。
    """
    try:
        placeholders = ",".join("?" * len(CANDIDATE_DIMENSIONS))
        rows = conn.execute(
            f"""
            SELECT m.memory_id, m.content, m.updated_at,
                   MIN(t.dimension_id) AS dimension
            FROM memories m
            JOIN memory_tags t ON t.memory_id = m.memory_id
            WHERE t.dimension_id IN ({placeholders})
              AND m.status = 'active'
              AND COALESCE(m.needs_review, 0) = 0
              AND COALESCE(m.route, 'memory') = 'memory'
              AND COALESCE(m.memory_type, '') != 'forget_request'
              AND (m.subject IS NULL OR m.subject = 'unknown'
                   OR lower(m.subject) = 'self')
              AND julianday(m.updated_at) > julianday('now') - ?
            GROUP BY m.memory_id
            ORDER BY CASE MIN(t.dimension_id) WHEN 'focus' THEN 0 ELSE 1 END,
                     m.updated_at DESC
            LIMIT ?
            """,
            (*CANDIDATE_DIMENSIONS, int(max(1, days)), int(max(1, limit)) * 3),
        ).fetchall()
    except Exception as e:  # noqa: BLE001——回指失败不得影响注入主流程
        logger.warning("last_mentions 取数失败（已忽略）: %s", e)
        return []

    exclude = exclude_contents or set()
    picked: list[dict[str, Any]] = []
    used = 0
    for r in rows:
        item = dict(r) if not isinstance(r, sqlite3.Row) else {k: r[k] for k in r.keys()}
        content = (item.get("content") or "").strip()
        if not content or content in exclude:
            continue
        # 预算：超预算即截断（宁少一条，不挤掉主块）
        if picked and used + len(content) > char_budget:
            break
        picked.append({
            "memory_id": item["memory_id"],
            "content": content,
            "dimension": item.get("dimension"),
            "updated_at": item.get("updated_at"),
        })
        used += len(content)
        if len(picked) >= limit:
            break
    return picked


def _fmt_date(updated_at: str | None) -> str:
    """ISO 时间戳 → MM-DD（回指只要「哪阵子的事」，不要完整时间戳占预算）。"""
    s = str(updated_at or "")
    if len(s) >= 10:
        return s[5:10]
    return s


def render(items: list[dict[str, Any]]) -> str:
    """渲染回指块文本（D-70 格式：``[上次提到] <日期> <一句话事实>``）。"""
    if not items:
        return ""
    lines = [f"[上次提到] {_fmt_date(it.get('updated_at'))} {it['content']}" for it in items]
    return "\n".join(lines)
