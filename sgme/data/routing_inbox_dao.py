"""data/routing_inbox_dao.py：三平面分流兜底表 routing_inbox 的读写层（v0.3 S1）。

设计位置：memory.db（与 memories 同库——分流决策发生在 L3 落库阶段，
跨库事务会带来两库不一致窗口）。

语义（v0.3 §3.4 / §4）：
- 提炼产出 confidence < 阈值、或归类不明确 → 写这里，**绝不静默丢**；
- 人工/维护流程处理后二选一：routed（指定落点）/ discarded（明确不要）；
- 已处理行永久保留（可溯源），不做物理删除。

幂等：同 (source_type, source_ref, content 前 120 字) 的 pending 行只留一条——
同一文件重跑不会把 inbox 撑爆；已处理行不参与去重（保留历史轨迹）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

#: 允许的 status 取值（与 DDL 的 CHECK 一致）
ALLOWED_STATUS = ("pending", "routed", "discarded")

#: 幂等锚用的正文前缀长度
_ANCHOR_PREFIX = 120


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create(
    conn: sqlite3.Connection,
    *,
    source_ref: str,
    source_type: str,
    content: str,
    route_guess: str | None = None,
    confidence: float = 0.0,
    reason: str | None = None,
    now: str | None = None,
) -> int | None:
    """写入一条待归类单元。返回 inbox_id；命中幂等返回既有 id；内容为空返回 None。"""
    text = (content or "").strip()
    if not text or not source_ref:
        return None
    prefix = text[:_ANCHOR_PREFIX]
    row = conn.execute(
        "SELECT inbox_id FROM routing_inbox "
        "WHERE status='pending' AND source_type=? AND source_ref=? "
        "AND substr(content,1,?) = ?",
        (source_type, source_ref, _ANCHOR_PREFIX, prefix),
    ).fetchone()
    if row is not None:
        return int(row[0])
    try:
        conf = float(confidence or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    cur = conn.execute(
        "INSERT INTO routing_inbox "
        "(source_ref, source_type, content, route_guess, confidence, reason, "
        " status, created_at) "
        "VALUES (?,?,?,?,?,?,'pending',?)",
        (source_ref, source_type, text, route_guess, conf, reason, now or _utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, inbox_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM routing_inbox WHERE inbox_id = ?", (int(inbox_id),)
    ).fetchone()
    return dict(row) if row is not None else None


def list_items(
    conn: sqlite3.Connection,
    *,
    status: str = "pending",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按状态列待归类单元（created_at 升序：先来先处理）。"""
    rows = conn.execute(
        "SELECT * FROM routing_inbox WHERE status = ? "
        "ORDER BY created_at ASC, inbox_id ASC LIMIT ? OFFSET ?",
        (status, int(limit), int(offset)),
    ).fetchall()
    return [dict(r) for r in rows]


def count(conn: sqlite3.Connection, *, status: str = "pending") -> int:
    row = conn.execute(
        "SELECT count(*) FROM routing_inbox WHERE status = ?", (status,)
    ).fetchone()
    return int(row[0]) if row else 0


def count_all(conn: sqlite3.Connection) -> dict[str, int]:
    """{status: count} 全量分组（运维看板用）。"""
    rows = conn.execute(
        "SELECT status, count(*) FROM routing_inbox GROUP BY status"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def resolve(
    conn: sqlite3.Connection,
    inbox_id: int,
    *,
    status: str,
    routed_to: str | None = None,
    resolved_by: str | None = None,
    now: str | None = None,
) -> bool:
    """收尾一条（routed / discarded）。仅 pending 可收尾——重复收尾返回 False。"""
    if status not in ("routed", "discarded"):
        raise ValueError(f"非法收尾状态: {status}")
    cur = conn.execute(
        "UPDATE routing_inbox SET status=?, routed_to=?, resolved_at=?, resolved_by=? "
        "WHERE inbox_id=? AND status='pending'",
        (status, routed_to, now or _utc_now(), resolved_by, int(inbox_id)),
    )
    conn.commit()
    return cur.rowcount > 0
