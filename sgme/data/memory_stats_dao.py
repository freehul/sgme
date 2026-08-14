"""storage/memory_stats_dao.py：memory.db 的 memory_stats sidecar DAO（v0.7 新增）。

A2 决策（边界严格，勿扩权）：
- **仅实现 `record_inject()`**：inject 链路命中记忆时写 `last_injected_at` + `recall_count += 1`。
- **不实现 `record_recall()`**：search 命中链路**禁止**挂载任何写操作（读路径不得引入写放大）。
- `last_recalled_at` 列照建但**留空**——预留字段，v0.8 待定。

best-effort 语义：统计写入失败（表缺失 / 库只读 / 并发锁）**绝不允许**打断 inject 主流程，
异常一律吞掉并记 WARNING 日志。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("sgme.data.memory_stats")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_inject(conn: sqlite3.Connection, memory_id: str, injected_at: str | None = None) -> bool:
    """记录一次注入：`last_injected_at = now()` 且 `recall_count += 1`（幂等 upsert）。

    Args:
        conn: memory.db 连接（memory_stats 与 memories 同库）。
        memory_id: 被注入的记忆 id。
        injected_at: 注入时刻（UTC ISO 8601），缺省取当前时间。

    Returns:
        True 表示统计已写入；False 表示 best-effort 失败（已记 WARNING，调用方不必处理）。

    Note:
        A2：本函数是 memory_stats 的**唯一**写入口。search 命中链路不得调用任何写函数。
    """
    ts = injected_at or _now_iso()
    try:
        conn.execute(
            """
            INSERT INTO memory_stats (memory_id, last_recalled_at, recall_count, last_injected_at)
            VALUES (?, NULL, 1, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
              recall_count = COALESCE(memory_stats.recall_count, 0) + 1,
              last_injected_at = excluded.last_injected_at
            """,
            (memory_id, ts),
        )
        conn.commit()
        return True
    except Exception as e:  # best-effort：统计失败绝不打断 inject 主流程
        logger.warning("memory_stats 写入失败（已忽略，不影响注入）：memory_id=%s, err=%s", memory_id, e)
        return False


def get_stats(conn: sqlite3.Connection, memory_id: str) -> dict | None:
    """读取单条记忆的使用统计（只读，供 admin/诊断使用）。不存在返回 None。"""
    cur = conn.execute("SELECT * FROM memory_stats WHERE memory_id=?", (memory_id,))
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))
