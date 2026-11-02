# -*- coding: utf-8 -*-
"""sgme/data/ingest_dao.py：ingest_tasks 任务表 CRUD（0.8 T-13 ingest 任务持久化）。

wiki ingest 任务由进程内 `_TASKS` 内存字典落库（wiki.db），服务重启后
queued/running 任务状态可恢复（图纸 `SGME-数据模型设计-v0.1.md` §二 wiki.db → ingest_tasks）。

状态机：queued → running → done / error。
当前路由直写 queued → done/error；running 为 DAO 支持的中间态（守护重试策略后续写入），
启动恢复对 running 任务按数据模型语义标记中断。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

# 状态常量（数据模型枚举：queued / running / done / error）
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
VALID_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_ERROR)


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_task(
    conn: sqlite3.Connection,
    task_id: str,
    source_type: str,
    source_ref: str,
    title: str | None = None,
) -> dict[str, Any]:
    """创建 ingest 任务（初始 status=queued），返回完整任务 dict。"""
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO ingest_tasks (task_id, source_type, source_ref, title,
                                  status, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (task_id, source_type, source_ref, title, STATUS_QUEUED, now, now),
    )
    conn.commit()
    return get_task(conn, task_id)  # type: ignore[return-value]


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    """按 task_id 查询任务；不存在返回 None。"""
    row = conn.execute(
        "SELECT * FROM ingest_tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def update_status(
    conn: sqlite3.Connection,
    task_id: str,
    status: str,
    error: str | None = None,
    page_id: str | None = None,
) -> bool:
    """流转任务状态（queued/running/done/error）。

    - done/error 终态同时落 finished_at；
    - page_id 落 result_page_id 列（对外接口映射为 page_id 字段）；
    - error/page_id 传 None 时保留原值；
    - 任务不存在返回 False。
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"非法任务状态: {status}")
    now = _now_iso()
    finished_at = now if status in (STATUS_DONE, STATUS_ERROR) else None
    cur = conn.execute(
        """
        UPDATE ingest_tasks
           SET status=?, error=COALESCE(?, error),
               result_page_id=COALESCE(?, result_page_id),
               updated_at=?, finished_at=COALESCE(?, finished_at)
         WHERE task_id=?
        """,
        (status, error, page_id, now, finished_at, task_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_tasks(
    conn: sqlite3.Connection,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """任务列表（updated_at 降序；status 可选过滤）——守护重试策略扫描入口。"""
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM ingest_tasks WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ingest_tasks ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def recover_interrupted_tasks(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """启动恢复（数据模型语义）：服务重启后修正上次进程遗留的任务状态。

    - status='queued'：置回 queued（可重跑）——保持原状，等待守护重试策略拾取；
    - status='running'：置 error（标记中断）——执行线程已随进程消亡，不能假装仍在跑；
    - done/error 终态不动（含既有 error 文案不被覆盖）。

    「可重跑 vs 标记中断」的取舍由守护重试策略决定（T-13 暂取上述双语义，
    数据模型 §二 ingest_tasks 启动时恢复段落）。

    Returns:
        {"kept_queued": 保持 queued 的任务数, "marked_error": 标记中断的任务数}
    """
    kept_queued = conn.execute(
        "SELECT COUNT(*) FROM ingest_tasks WHERE status=?", (STATUS_QUEUED,)
    ).fetchone()[0]
    now = _now_iso()
    cur = conn.execute(
        """
        UPDATE ingest_tasks
           SET status=?, error=?, updated_at=?, finished_at=?
         WHERE status=?
        """,
        (STATUS_ERROR, "服务重启，任务中断", now, now, STATUS_RUNNING),
    )
    conn.commit()
    return {"kept_queued": kept_queued, "marked_error": cur.rowcount}
