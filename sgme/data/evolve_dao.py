# -*- coding: utf-8 -*-
"""sgme/data/evolve_dao.py：wiki_evolve 自进化进度表 CRUD（W4 方案 v0.3 §5.4）。

独立游标：与 memory 提炼的 refine_cursor / raw_files.status **完全分离**——
evolve 只认本表记录的 session_key，避免与 memory 提炼抢消费进度/重复提炼
（P0-2 审查修正）。

状态机：queued → done / skipped / rejected / error。
- skipped：费用门禁未过（会话过短）或已处理（幂等）
- rejected：规则闸门拦截（非法条目）
- done：提炼完成（action=appended/created/noop）
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

STATUS_QUEUED = "queued"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"
VALID_STATUSES = (STATUS_QUEUED, STATUS_DONE, STATUS_SKIPPED, STATUS_REJECTED, STATUS_ERROR)


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_run(conn: sqlite3.Connection, session_key: str) -> dict[str, Any]:
    """登记一次自进化运行（幂等：session_key 已存在则保留，不覆盖）。"""
    now = _now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO wiki_evolve (session_key, status, created_at, processed_at)
        VALUES (?,?,?,?)
        """,
        (session_key, STATUS_QUEUED, now, now),
    )
    conn.commit()
    return get_run(conn, session_key) or {}


def get_run(conn: sqlite3.Connection, session_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM wiki_evolve WHERE session_key=?", (session_key,)
    ).fetchone()
    return dict(row) if row is not None else None


def has_run(conn: sqlite3.Connection, session_key: str) -> bool:
    return get_run(conn, session_key) is not None


def update_run(
    conn: sqlite3.Connection,
    session_key: str,
    *,
    status: str,
    action: str | None = None,
    entry_hash: str | None = None,
    page_id: str | None = None,
    error: str | None = None,
) -> bool:
    """流转状态（终态落 processed_at）。"""
    if status not in VALID_STATUSES:
        raise ValueError(f"非法状态: {status}")
    cur = conn.execute(
        """
        UPDATE wiki_evolve
           SET status=?, action=COALESCE(?, action),
               entry_hash=COALESCE(?, entry_hash),
               page_id=COALESCE(?, page_id),
               error=COALESCE(?, error),
               processed_at=?
         WHERE session_key=?
        """,
        (status, action, entry_hash, page_id, error, _now_iso(), session_key),
    )
    conn.commit()
    return cur.rowcount > 0


def list_runs(
    conn: sqlite3.Connection, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """运行记录（processed_at 降序；status 可选过滤）——审计/重试扫描入口。"""
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM wiki_evolve WHERE status=? ORDER BY processed_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM wiki_evolve ORDER BY processed_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
