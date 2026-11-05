"""operations/cards.py：core 记忆卡操作层（D-79，2026-11-05）。

core 层是「稳定的人属性」，写入只有三种来源：用户对话授权（agent 代改）、
WebUI 编辑、脚本初始化。**不参与自动批量提炼**——模型不自行决定写卡。

分层（同项目约定）：本模块只做协议翻译与错误映射，
SQL 与校验在 data/memory_card_dao.py。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sgme.data import memory_card_dao as dao
from sgme.data.db import CARD_MAX_CHARS, CARD_SCHEMA, SLOT_MAX_CHARS
from sgme.operations.errors import ERR_INVALID_ARGS, ERR_NOT_FOUND, OperationResult

MAX_HISTORY = 200


def schema() -> OperationResult:
    """槽位定义（供 agent / WebUI 知道能写什么、能写多长）。"""
    return OperationResult.succeed({
        "cards": [
            {
                "card_id": cid,
                "title": spec["title"],
                "slots": spec["slots"],
                "multi": spec.get("multi") or [],
            }
            for cid, spec in CARD_SCHEMA.items()
        ],
        "limits": {"card_max_chars": CARD_MAX_CHARS, "slot_max_chars": SLOT_MAX_CHARS},
    })


def list_cards(mem_conn: sqlite3.Connection) -> OperationResult:
    """全部卡片（未填的也在列表里，filled=false）。"""
    try:
        cards = dao.list_cards(mem_conn)
    except Exception as e:  # noqa: BLE001
        return OperationResult.fail(ERR_INVALID_ARGS, f"卡片读取失败: {e}")
    return OperationResult.succeed({"cards": cards, "total": len(cards)})


def get_card(mem_conn: sqlite3.Connection, card_id: str) -> OperationResult:
    """单卡；未填过 → filled=false（不算错误）。"""
    try:
        row = dao.get_card(mem_conn, card_id)
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    except Exception as e:  # noqa: BLE001
        return OperationResult.fail(ERR_INVALID_ARGS, f"卡片读取失败: {e}")
    if row is None:
        spec = CARD_SCHEMA[card_id]
        return OperationResult.succeed({
            "card_id": card_id, "title": spec["title"], "slots": {}, "filled": False,
        })
    return OperationResult.succeed({**row, "filled": True})


def update_slot(
    mem_conn: sqlite3.Connection,
    card_id: str,
    slot_key: str,
    value: Any = None,
    *,
    reason: str | None = None,
    changed_by: str = "user",
) -> OperationResult:
    """写一个槽位（覆盖式）+ 留痕。value 传 None/空 = 清除该槽位。

    Raises（转错误码）：
        ERR_INVALID_ARGS: 未知卡片/槽位、类型不符、超硬上限
        ERR_NOT_FOUND: 卡片不存在（理论上 CARD_SCHEMA 已挡，防御性保留）
    """
    try:
        out = dao.upsert_slot(
            mem_conn, card_id, slot_key, value, changed_by=changed_by, reason=reason,
        )
    except dao.CardLimitError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    except Exception as e:  # noqa: BLE001
        return OperationResult.fail(ERR_INVALID_ARGS, f"卡片写入失败: {e}")
    return OperationResult.succeed(out)


def history(
    mem_conn: sqlite3.Connection,
    card_id: str | None = None,
    limit: int = 50,
) -> OperationResult:
    """修改历史（最新在前）。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return OperationResult.fail(ERR_INVALID_ARGS, f"limit 必须 ≥ 1: {limit!r}")
    if card_id and card_id not in CARD_SCHEMA:
        return OperationResult.fail(ERR_NOT_FOUND, f"未知卡片: {card_id}")
    rows = dao.list_history(mem_conn, card_id=card_id, limit=min(limit, MAX_HISTORY))
    return OperationResult.succeed({"items": rows, "total": len(rows)})
