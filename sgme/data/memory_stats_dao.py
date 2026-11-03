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
    """记录一次注入：`last_injected_at = now`，**按不同自然日**给 `recall_count` 计数。

    Args:
        conn: memory.db 连接（memory_stats 与 memories 同库）。
        memory_id: 被注入的记忆 id。
        injected_at: 注入时刻（UTC ISO 8601），缺省取当前时间。

    Returns:
        True 表示统计已写入；False 表示 best-effort 失败（已记 WARNING，调用方不必处理）。

    Note:
        **D-73 自然日去重（2026-11-03）**：原实现每次注入都 ``recall_count += 1``，
        而 D-73 的幂律放大把 ``recall_count`` 当「被复习的天数」（间隔重复的
        distinct_days）——同一轮对话里反复注入会把它刷爆，有效期被虚假放大到 3× 上限。
        现在：同一自然日内的重复注入**只刷新 `last_injected_at`、不计数**；
        跨日才 +1，并把 `last_recalled_at` 记为该次计入时刻（原先恒 NULL）。
        自然日按 UTC 判（项目全链路 UTC，不做本地时区解释）。

        A2：本函数是 memory_stats 的**唯一**写入口。search 命中链路不得调用任何写函数。
    """
    ts = injected_at or _now_iso()
    try:
        conn.execute(
            """
            INSERT INTO memory_stats (memory_id, last_recalled_at, recall_count, last_injected_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
              recall_count = CASE
                WHEN date(memory_stats.last_injected_at) = date(excluded.last_injected_at)
                THEN COALESCE(memory_stats.recall_count, 0)
                ELSE COALESCE(memory_stats.recall_count, 0) + 1
              END,
              last_recalled_at = CASE
                WHEN date(memory_stats.last_injected_at) = date(excluded.last_injected_at)
                THEN memory_stats.last_recalled_at
                ELSE excluded.last_injected_at
              END,
              last_injected_at = excluded.last_injected_at
            """,
            (memory_id, ts, ts),
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
