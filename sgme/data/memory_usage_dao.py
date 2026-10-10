"""memory_usage_dao.py：记忆调用统计 DAO（T-251，2026-10-10）。

数据流：双源埋点（检索命中 search + 画像注入 inject）→ ``record_usage()``
（按日聚合 upsert 累加）→ ``GET /v1/admin/memories/usage`` 查询。

与既有两层调用统计**正交并存**，互不替代：
- ``api_usage_daily``（T-163）：端点 / MCP 工具维度（对象名被路由模板归一化丢弃）；
- ``skill_usage_daily``（T-174）：技能消费维度；
- 本表（第三层）：**记忆对象维度**——「哪些记忆常被用 / 从未被用过」。

A2 政策修订（2026-10-10 用户批）：
- v0.7 A2 原文「search 命中链路禁止挂载任何写操作」放宽为**仅限本表**——
  检索链路可写 ``memory_usage_daily``（单次批量 upsert + 日聚合，控写放大）；
  ``memory_stats`` / ``last_recalled_at`` 链路照旧禁止（不受本次放宽影响）。
- inject 侧注入集口径：以 ``section_results`` 为权威（T-203 G1 在案）；
  既有 ``memory_stats.record_inject`` 链路保持原样，本表与之正交。

设计要点：
- 表 ``memory_usage_daily`` 见 ``db.py`` 的 ``MEMORY_USAGE_DDL``；本模块是唯一读写方
- 写入方全静默（best-effort，绝不抛）：统计是旁路，任何失败不得影响主链路
- 行粒度 = (day, memory_id, source, caller)：一天一记忆一来源一调用方一行，
  upsert 累加；空间 O(天 × 记忆 × 来源 × 调用方)，保留 400 天由
  ``prune_memory_usage`` 控制
- day = UTC（对齐 usage_dao）；caller 取值同 api_usage_daily：
  agent_id / default / anonymous / unknown

分层：本模块只做 SQL，不做业务判断（caller 解析在入口层，参数校验在 operations 层）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("sgme.data.memory_usage")

#: 双源白名单（与 db.py MEMORY_USAGE_DDL 注释一致）
SOURCES = ("search", "inject")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rows_to_dicts(cur: sqlite3.Cursor) -> list[dict]:
    """结果集 → dict 列表。

    连接未开 ``row_factory = sqlite3.Row`` 时兜底（裸 sqlite3.connect 调用形态，
    照 session_dao 先例）——不依赖外部连接设置。
    """
    rows = cur.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], sqlite3.Row):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def record_usage(
    conn: sqlite3.Connection,
    memory_ids: list[str],
    source: str,
    caller: str,
    ts: str | None = None,
) -> bool:
    """记录一批记忆调用（按 day+memory_id+source+caller 聚合累加，幂等 upsert）。

    Args:
        conn: memory.db 连接（调用方持有；本函数自 commit）
        memory_ids: 被调用的记忆 id 列表（去重保序；空列表直接返回 True 不写）
        source: ``search``（检索命中）或 ``inject``（画像注入）；其他值抛 ValueError
        caller: 调用方标识（agent_id / default / anonymous / unknown；
            未解析出请求方时由调用方回落 ``unknown``）
        ts: ISO UTC 时间戳（默认当前；测试可注入），day 取 ``ts[:10]``

    Returns:
        True 表示写入成功（或空表无需写入）；False 表示 best-effort 失败
        （已记 WARNING，调用方不必处理）。

    Raises:
        ValueError: source 不在白名单内（参数错误，调用方应自行收敛）。
    """
    if source not in SOURCES:
        raise ValueError(f"未知来源: {source!r}（可选: {'/'.join(SOURCES)}）")
    ids = [m for m in dict.fromkeys(memory_ids or []) if m]
    if not ids:
        return True
    ts = ts or _now_iso()
    day = ts[:10]
    try:
        conn.executemany(
            """
            INSERT INTO memory_usage_daily (day, memory_id, source, caller, hits, last_ts)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(day, memory_id, source, caller)
            DO UPDATE SET hits = hits + 1,
                          last_ts = excluded.last_ts
            """,
            [(day, mid, source, caller, ts) for mid in ids],
        )
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001 —— best-effort：统计失败绝不打断主链路
        logger.warning(
            "memory_usage_daily 写入失败（已忽略，不影响主链路）：source=%s, err=%s",
            source,
            e,
        )
        return False


def query_usage(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    source: str | None = None,
    caller: str | None = None,
    memory_id: str | None = None,
    limit: int = 200,
    order: str = "hits",
) -> list[dict]:
    """查询近 N 天记忆调用统计：按「记忆 × 来源」聚合。

    - LEFT JOIN memories 取 ``content_head``（内容前 80 字）/ ``memory_type`` /
      ``status``——记忆已归档 / 被取代 / 不在池中时相应列为 NULL（调用事实仍保留）
    - ``order``：``hits``（次数降序，默认）或 ``last_ts``（最近调用降序）
    """
    since = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).date().isoformat()
    sql = """
        SELECT u.memory_id, u.source,
               SUM(u.hits) AS hits,
               MAX(u.last_ts) AS last_ts,
               MAX(u.day) AS last_day,
               substr(m.content, 1, 80) AS content_head,
               m.memory_type AS memory_type,
               m.status AS status
        FROM memory_usage_daily u
        LEFT JOIN memories m ON m.memory_id = u.memory_id
        WHERE u.day >= ?
    """
    params: list = [since]
    if source:
        sql += " AND u.source = ?"
        params.append(source)
    if caller:
        sql += " AND u.caller = ?"
        params.append(caller)
    if memory_id:
        sql += " AND u.memory_id = ?"
        params.append(memory_id)
    sql += " GROUP BY u.memory_id, u.source"
    if order == "last_ts":
        sql += " ORDER BY last_ts DESC, hits DESC, u.memory_id"
    else:
        sql += " ORDER BY hits DESC, last_ts DESC, u.memory_id"
    sql += " LIMIT ?"
    params.append(max(1, int(limit)))
    return _rows_to_dicts(conn.execute(sql, params))


def query_unused(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    limit: int = 500,
) -> list[dict]:
    """窗口内零调用的 active 记忆清单（活跃度分析的另一半：从未被用 / 已冷）。

    LEFT JOIN 只看窗口内（``day >= cutoff``）的调用行——窗口外旧调用不影响判定：
    窗口内无调用即视为「本窗口未被使用」。created_at DESC 排序。
    """
    since = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).date().isoformat()
    sql = """
        SELECT m.memory_id, m.memory_type,
               substr(m.content, 1, 80) AS content_head,
               m.created_at, m.updated_at
        FROM memories m
        LEFT JOIN memory_usage_daily u
               ON u.memory_id = m.memory_id AND u.day >= ?
        WHERE m.status = 'active' AND u.memory_id IS NULL
        ORDER BY m.created_at DESC
        LIMIT ?
    """
    return _rows_to_dicts(conn.execute(sql, (since, max(1, int(limit)))))


def prune_memory_usage(conn: sqlite3.Connection, keep_days: int = 400) -> int:
    """删除超过保留期（默认 400 天，对齐 api_usage_daily）的日行；返回删除行数。"""
    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).date().isoformat()
    cur = conn.execute("DELETE FROM memory_usage_daily WHERE day < ?", (cutoff,))
    conn.commit()
    return cur.rowcount or 0
