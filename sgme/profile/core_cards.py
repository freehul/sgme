"""profile/core_cards.py：core 记忆卡渲染成注入块（D-79，2026-11-05）。

三层记忆分工的 core 层：把 memory_cards 里的人属性渲染成一段固定前缀块，
**每次注入必带、排在最前面**。

纪律：
- 零 LLM：纯表查询 + 模板拼接（守「注入零 LLM」铁律）
- 空卡不渲染、不补位、不猜
- 与角色卡 character_book 分开：本块标题固定为「关于你」，角色块另有其名
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sgme.data import memory_card_dao
from sgme.data.db import CARD_SCHEMA

# 单卡内多值槽的展示前缀（列表项）
BULLET = "· "


def _card_lines(card: dict) -> list[str]:
    """一张卡 → 若干文本行（跳过空槽）。"""
    spec = CARD_SCHEMA.get(card["card_id"]) or {}
    slots = card.get("slots") or {}
    lines: list[str] = []
    for key in spec.get("slots") or []:
        val = slots.get(key)
        if not val:
            continue
        if isinstance(val, list):
            lines.append(f"{key}：")
            lines.extend(f"  {BULLET}{x}" for x in val)
        else:
            lines.append(f"{key}：{val}")
    return lines


def build_core_block(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """构建「关于你」core 块。

    Returns:
        {"block": {title/items/present}, "tokens": int}；一张卡都没填时返回 None。
    """
    cards = memory_card_dao.list_cards(conn)
    items: list[dict] = []
    for card in cards:
        lines = _card_lines(card)
        if not lines:
            continue
        items.append({
            "card_id": card["card_id"],
            "title": card["title"],
            "content": "\n".join(lines),
            "updated_at": card.get("updated_at"),
        })
    if not items:
        return None
    total_chars = sum(len(i["content"]) for i in items)
    return {
        "block": {
            "title": "👤 关于你",
            "items": items,
            "present": True,
            "kind": "core_cards",
        },
        # 中文按 1.5 字/token 粗估（与项目既有估算口径一致）
        "tokens": int(total_chars / 1.5) + 8,
    }


def render_plain(block: dict[str, Any] | None) -> str:
    """调试/终端用：把块渲染成纯文本。"""
    if not block:
        return ""
    out = [block["block"]["title"]]
    for it in block["block"]["items"]:
        out.append(f"▪ {it['title']}")
        out.append(it["content"])
    return "\n".join(out)
