"""data/memory_card_dao.py：core 记忆卡 DAO（D-79，2026-11-05）。

三层记忆分工的 core 层——只装「稳定的人属性」，每次注入必带：

- 一槽一值：写同卡同槽位 = 覆盖，旧值进 memory_card_history（可溯源，不删）
- 硬上限：单卡 ≤400 字、单槽 ≤120 字，超限拒绝（防退化成段落）
- 不参与自动批量提炼：写入来源只有 user / agent / webui 三种，记在 updated_by

槽位结构由 db.CARD_SCHEMA 定义（唯一真源），本模块只存值。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sgme.data.db import CARD_MAX_CHARS, CARD_SCHEMA, SLOT_MAX_CHARS

logger = logging.getLogger("sgme.data.memory_card")


class CardLimitError(ValueError):
    """超出硬上限（单卡/单槽字数）——调用方转 ERR_INVALID_ARGS 并记告警。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], sqlite3.Row):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _decode(row: dict | None) -> dict | None:
    """slots JSON 容错解析。"""
    if row is None:
        return None
    try:
        row["slots"] = json.loads(row.get("slots") or "{}")
    except (json.JSONDecodeError, TypeError):
        logger.warning("memory_cards.slots 解析失败（card=%s），按空卡处理", row.get("card_id"))
        row["slots"] = {}
    return row


def list_cards(conn: sqlite3.Connection) -> list[dict]:
    """全部卡片（按 CARD_SCHEMA 顺序，缺卡不补——渲染侧负责跳过空卡）。"""
    rows = {r["card_id"]: _decode(r) for r in _fetch_all(conn, "SELECT * FROM memory_cards", ())}
    out: list[dict] = []
    for card_id, spec in CARD_SCHEMA.items():
        row = rows.get(card_id)
        out.append({
            "card_id": card_id,
            "title": (row or {}).get("title") or spec["title"],
            "slots": (row or {}).get("slots") or {},
            "updated_at": (row or {}).get("updated_at"),
            "updated_by": (row or {}).get("updated_by"),
            "version": (row or {}).get("version", 0),
            "filled": bool(row),
        })
    return out


def get_card(conn: sqlite3.Connection, card_id: str) -> dict | None:
    """单卡；未填过返回 None（不是空卡）。"""
    spec = CARD_SCHEMA.get(card_id)
    if spec is None:
        raise ValueError(f"未知卡片: {card_id}（允许: {list(CARD_SCHEMA)}）")
    return _decode(_fetch_one(conn, "SELECT * FROM memory_cards WHERE card_id=?", (card_id,)))


def _card_chars(slots: dict) -> int:
    total = 0
    for v in slots.values():
        if isinstance(v, list):
            total += sum(len(str(x)) for x in v)
        else:
            total += len(str(v))
    return total


def upsert_slot(
    conn: sqlite3.Connection,
    card_id: str,
    slot_key: str,
    value: Any,
    *,
    changed_by: str = "user",
    reason: str | None = None,
) -> dict:
    """写一个槽位（覆盖式）+ 记历史。

    value 为 None / 空串 / 空列表 = 清除该槽位（历史留旧值）。
    multi 槽（CARD_SCHEMA.multi）接受字符串列表，其余槽位只接受字符串。

    Raises:
        ValueError: 未知卡片/槽位、类型不符
        CardLimitError: 超单槽或单卡字数上限
    """
    spec = CARD_SCHEMA.get(card_id)
    if spec is None:
        raise ValueError(f"未知卡片: {card_id}（允许: {list(CARD_SCHEMA)}）")
    if slot_key not in spec["slots"]:
        raise ValueError(
            f"未知槽位: {card_id}.{slot_key}（允许: {spec['slots']}）"
        )

    multi = slot_key in (spec.get("multi") or [])
    if multi:
        if value is None:
            norm: Any = []
        elif isinstance(value, str):
            norm = [value] if value.strip() else []
        elif isinstance(value, (list, tuple)):
            norm = [str(x).strip() for x in value if str(x).strip()]
        else:
            raise ValueError(f"{card_id}.{slot_key} 需要字符串或字符串数组")
        chars = sum(len(x) for x in norm)
    else:
        if isinstance(value, (list, tuple)):
            raise ValueError(f"{card_id}.{slot_key} 是单值槽，不接受数组")
        norm = "" if value is None else str(value).strip()
        chars = len(norm)

    if chars > SLOT_MAX_CHARS:
        raise CardLimitError(
            f"槽位 {card_id}.{slot_key} 超上限（{chars}/{SLOT_MAX_CHARS} 字）"
        )

    row = get_card(conn, card_id)
    slots: dict = dict((row or {}).get("slots") or {})
    old = slots.get(slot_key)
    old_text = json.dumps(old, ensure_ascii=False) if isinstance(old, list) else (old or None)

    if norm == "" or (multi and not norm):
        slots.pop(slot_key, None)
        new_text = None
    else:
        slots[slot_key] = norm
        new_text = json.dumps(norm, ensure_ascii=False) if multi else str(norm)

    total = _card_chars(slots)
    if total > CARD_MAX_CHARS:
        raise CardLimitError(
            f"卡片 {card_id} 超上限（{total}/{CARD_MAX_CHARS} 字），请精简其他槽位"
        )

    now = _now_iso()
    version = int((row or {}).get("version") or 0) + 1
    title = spec["title"]
    conn.execute(
        """
        INSERT INTO memory_cards (card_id, title, slots, updated_at, updated_by, version)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(card_id) DO UPDATE SET
          title=excluded.title, slots=excluded.slots, updated_at=excluded.updated_at,
          updated_by=excluded.updated_by, version=excluded.version
        """,
        (card_id, title, json.dumps(slots, ensure_ascii=False), now, changed_by, version),
    )
    conn.execute(
        """
        INSERT INTO memory_card_history
          (card_id, slot_key, old_value, new_value, changed_at, changed_by, reason)
        VALUES (?,?,?,?,?,?,?)
        """,
        (card_id, slot_key, old_text, new_text, now, changed_by, reason),
    )
    conn.commit()
    logger.info("card slot 更新: %s.%s（by=%s, chars=%d）", card_id, slot_key, changed_by, chars)
    return {
        "card_id": card_id,
        "slot_key": slot_key,
        "value": norm,
        "version": version,
        "updated_at": now,
    }


def list_history(
    conn: sqlite3.Connection,
    card_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """修改历史（最新在前）。"""
    if card_id:
        return _fetch_all(
            conn,
            "SELECT * FROM memory_card_history WHERE card_id=? ORDER BY id DESC LIMIT ?",
            (card_id, int(limit)),
        )
    return _fetch_all(
        conn,
        "SELECT * FROM memory_card_history ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )
