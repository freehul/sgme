"""server/routes_cards.py：core 记忆卡端点（D-79，2026-11-05）。

业务实现全部在 sgme.operations.cards，本文件只做协议翻译。

端点：
    GET  /v1/admin/cards/schema                  槽位定义（能写什么、多长）
    GET  /v1/admin/cards                         全部卡片
    GET  /v1/admin/cards/history                 修改历史（全卡）
    GET  /v1/admin/cards/{card_id}               单卡
    GET  /v1/admin/cards/{card_id}/history       单卡修改历史
    POST /v1/admin/cards/{card_id}/slots/{slot_key}  写一个槽位（覆盖式 + 留痕）

⚠️ 路由顺序：/cards/history 必须声明在 /cards/{card_id} 之前，
否则 "history" 会被当成 card_id 吃掉。

鉴权：require_agent_key（与 review 同档——agent 代用户改卡是正常路径）。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from sgme.operations.cards import (
    get_card as get_card_operation,
    history as history_operation,
    list_cards as list_cards_operation,
    schema as schema_operation,
    update_slot as update_slot_operation,
)
from sgme.server.app import require_agent_key, run_operation

router = APIRouter()


@router.get("/v1/admin/cards/schema")
def cards_schema(_: str = Depends(require_agent_key)):
    """槽位定义（六张卡各自允许哪些槽位、多值槽有哪些、字数上限）。"""
    return run_operation(schema_operation)


@router.get("/v1/admin/cards/history")
def cards_history(
    request: Request,
    limit: str | None = None,
    _: str = Depends(require_agent_key),
):
    """修改历史（全卡，最新在前）。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    try:
        n = int(limit) if limit is not None else 50
    except (TypeError, ValueError):
        n = 50
    return run_operation(history_operation, mem_conn, None, limit=n)


@router.get("/v1/admin/cards")
def list_cards(request: Request, _: str = Depends(require_agent_key)):
    """全部卡片（未填的也在，filled=false）。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(list_cards_operation, mem_conn)


@router.get("/v1/admin/cards/{card_id}")
def get_card(request: Request, card_id: str, _: str = Depends(require_agent_key)):
    """单卡。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(get_card_operation, mem_conn, card_id)


@router.get("/v1/admin/cards/{card_id}/history")
def card_history(
    request: Request,
    card_id: str,
    limit: str | None = None,
    _: str = Depends(require_agent_key),
):
    """单卡修改历史。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    try:
        n = int(limit) if limit is not None else 50
    except (TypeError, ValueError):
        n = 50
    return run_operation(history_operation, mem_conn, card_id, limit=n)


@router.post("/v1/admin/cards/{card_id}/slots/{slot_key}")
def update_slot(
    request: Request,
    card_id: str,
    slot_key: str,
    body: dict | None = None,
    _: str = Depends(require_agent_key),
):
    """写一个槽位（覆盖式，旧值进历史）。

    Body：value（字符串或字符串数组；null/空 = 清除该槽位）、
          reason（可选，写进历史）、changed_by（可选，默认 user）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    b = body or {}
    return run_operation(
        update_slot_operation,
        mem_conn,
        card_id,
        slot_key,
        b.get("value"),
        reason=b.get("reason"),
        changed_by=b.get("changed_by") or "user",
    )
