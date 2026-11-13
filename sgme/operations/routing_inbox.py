"""operations/routing_inbox.py：三平面分流兜底队列的操作层（v0.3 S1，2026-11-14）。

三段式（照 operations/health.py、operations/config.py 样板）：
1. 操作函数返回协议无关的信息超集（OperationResult）
2. 投影函数把超集裁成各入口的历史契约形态
3. HTTP / MCP 入口层只做协议翻译

承接入口：
======================================  ==========================
HTTP GET  /v1/admin/inbox                list_inbox + http_payload
HTTP POST /v1/admin/inbox/{id}/route     resolve_inbox(routed)
HTTP POST /v1/admin/inbox/{id}/discard   resolve_inbox(discarded)
======================================  ==========================

为什么需要这一层：分流兜底是「不丢」的最后一道闸，处理动作要可审计
（谁在什么时候把它归到哪儿）——DAO 只记结果，操作层负责参数校验与错误码。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from sgme.data import routing_inbox_dao as dao
from sgme.operations.errors import ERR_INVALID_ARGS, ERR_NOT_FOUND, OperationResult


def _parse_limit(limit: Any) -> int:
    """limit 归一：1..500，缺省 50；非法抛 ValueError（由调用方转 ERR_INVALID_ARGS）。"""
    try:
        value = int(limit if limit is not None else 50)
    except (TypeError, ValueError):
        raise ValueError("limit 必须为正整数")
    if value <= 0:
        raise ValueError("limit 必须为正整数")
    return min(value, 500)


def _parse_offset(offset: Any) -> int:
    try:
        value = int(offset if offset is not None else 0)
    except (TypeError, ValueError):
        raise ValueError("offset 必须为非负整数")
    if value < 0:
        raise ValueError("offset 必须为非负整数")
    return value


def list_inbox(
    mem_conn: sqlite3.Connection,
    *,
    status: str = "pending",
    limit: Any = 50,
    offset: Any = 0,
) -> OperationResult:
    """列待归类单元。"""
    if status not in dao.ALLOWED_STATUS:
        return OperationResult.fail(
            ERR_INVALID_ARGS,
            f"非法 status: {status}（可用: {list(dao.ALLOWED_STATUS)}）",
        )
    try:
        lim = _parse_limit(limit)
        off = _parse_offset(offset)
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    items = dao.list_items(mem_conn, status=status, limit=lim, offset=off)
    return OperationResult.succeed({
        "items": items,
        "count": len(items),
        "total": dao.count(mem_conn, status=status),
        "by_status": dao.count_all(mem_conn),
        "status": status,
        "limit": lim,
        "offset": off,
    })


def resolve_inbox(
    mem_conn: sqlite3.Connection,
    inbox_id: Any,
    *,
    action: str,
    routed_to: str | None = None,
    resolved_by: str | None = None,
) -> OperationResult:
    """收尾一条待归类单元。action：routed / discarded。

    routed 必须给 routed_to（落到哪儿），否则无从溯源——这是硬校验。
    """
    try:
        iid = int(inbox_id)
    except (TypeError, ValueError):
        return OperationResult.fail(ERR_INVALID_ARGS, "inbox_id 必须为整数")
    if action not in ("routed", "discarded"):
        return OperationResult.fail(
            ERR_INVALID_ARGS, "action 只能是 routed 或 discarded"
        )
    if action == "routed" and not (routed_to or "").strip():
        return OperationResult.fail(ERR_INVALID_ARGS, "routed 必须提供 routed_to")
    row = dao.get(mem_conn, iid)
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"inbox 记录不存在: {iid}")
    ok = dao.resolve(
        mem_conn, iid,
        status=action,
        routed_to=(routed_to or "").strip() or None,
        resolved_by=resolved_by,
    )
    if not ok:
        return OperationResult.fail(
            ERR_INVALID_ARGS,
            f"inbox 记录已处理（当前 status={row.get('status')}），不重复收尾",
        )
    return OperationResult.succeed({
        "inbox_id": iid,
        "status": action,
        "routed_to": (routed_to or "").strip() or None,
        "before_status": row.get("status"),
    })


def inbox_stats(mem_conn: sqlite3.Connection) -> OperationResult:
    """分组计数（health / 看板取数用）。"""
    return OperationResult.succeed({"by_status": dao.count_all(mem_conn)})


def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """HTTP 形态：操作层信息超集即响应体（与 v0.7 三段式一致，恒等投影）。"""
    return data
