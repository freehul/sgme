"""profile/persona_block.py：注入性格参考块（ST-35 T-101）。

从 persona_traits 取高置信度 active 特质拼装「性格参考」block。
准入门槛（Backlog ST-35 AC）：confidence >= 0.45 且 evidence_count >= 3——
低置信度不进 prompt 省 token，也避免一两条记忆就给 agent 错误的性格印象。

措辞纪律：输出用「倾向」，禁止标签式判决。
"""

from __future__ import annotations

import sqlite3
from typing import Any

# 注入门槛：低于任一阈值不注入该特质
MIN_CONFIDENCE = 0.45
MIN_EVIDENCE = 3

# 单块最多特质条数（控 token）
MAX_TRAITS = 6


def build_persona_block(
    mem_conn: sqlite3.Connection,
    *,
    scene_context: str | None = None,
    min_confidence: float = MIN_CONFIDENCE,
    min_evidence: int = MIN_EVIDENCE,
) -> dict[str, Any] | None:
    """构建性格参考 block。

    Returns:
        {"block": {title/items/present}, "tokens": int}；无可注入特质返回 None。

    特质筛选：先取 general 情境，不足时补指定情境（scene_context 参数）。
    """
    from sgme.data import persona_dao

    traits = persona_dao.list_traits(
        mem_conn,
        scene_context=scene_context or "general",
        min_confidence=min_confidence,
        limit=MAX_TRAITS * 2,
    )
    # 证据数门槛在 confidence 过滤后二次过滤（list_traits 只支持置信度参数）
    traits = [t for t in traits if t["evidence_count"] >= min_evidence]
    # 跨维度去重：每维度只取置信度最高的一条
    by_dim: dict[str, dict] = {}
    for t in traits:
        by_dim.setdefault(t["dimension"], t)
    picked = list(by_dim.values())[:MAX_TRAITS]
    if not picked:
        return None

    items = []
    for t in picked:
        level = "高" if t["confidence"] >= 0.75 else ("中" if t["confidence"] >= 0.55 else "初步")
        items.append({
            "content": f"{t['dimension']}：倾向「{t['value']}」（{level}置信）",
        })
    return {
        "block": {
            "title": "性格参考",
            "items": items,
            "present": True,
        },
        "tokens": len(items) * 25,
    }
