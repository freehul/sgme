"""usage_dao.py：接口调用统计 DAO（T-163，2026-09-13）。

数据流：HTTP 中间件（9910）/ MCP 中间件（9913）→ ``record_usage()``
（按日聚合 upsert 累加）→ ``GET /v1/admin/usage`` 查询。

设计要点：
- 表 ``api_usage_daily`` 见 ``db.py`` 的 ``API_USAGE_DDL``；本模块是唯一读写方
- 写入方全静默（调用点 try/except）：统计是旁路，任何失败不得影响主链路
- 行粒度 = (day, kind, name, caller)：一天一个端点一个调用方一行，upsert 累加；
  空间 O(天 × 端点 × 调用方)，400 天由 ``prune_usage`` 控制
- caller 取值：agent_id（鉴权 key 反查，见 AgentKeyStore.resolve_agent_id）/
  "default"（env 主 key）/ "anonymous"（无 key）/ "unknown"（key 无法反查）

分层：本模块只做 SQL，不做业务判断（caller 解析在入口层完成）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def record_usage(
    conn: sqlite3.Connection,
    kind: str,
    name: str,
    caller: str,
    ip: str | None = None,
    *,
    ts: str | None = None,
) -> None:
    """记录一次调用（按 day+kind+name+caller 聚合累加，幂等 upsert）。

    Args:
        conn: memory.db 连接（调用方持有；本函数自 commit）
        kind: "http"（9910 端点）或 "mcp"（9913 工具）
        name: HTTP 路由模板（如 /v1/admin/demands/{demand_id}）或 MCP 工具名
        caller: 调用方标识（agent_id / default / anonymous / unknown）
        ip: 来源 IP（可选，记录最近一次）
        ts: ISO UTC 时间戳（默认当前；测试可注入）
    """
    ts = ts or _now_iso()
    day = ts[:10]
    conn.execute(
        """
        INSERT INTO api_usage_daily (day, kind, name, caller, calls, last_ts, last_ip)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(day, kind, name, caller)
        DO UPDATE SET calls = calls + 1,
                      last_ts = excluded.last_ts,
                      last_ip = excluded.last_ip
        """,
        (day, kind, name, caller, ts, ip),
    )
    conn.commit()


def query_usage(
    conn: sqlite3.Connection,
    days: int = 30,
    kind: str | None = None,
) -> list[dict]:
    """查询近 N 天调用统计：按 name × caller 汇总（次数降序）。"""
    since = (datetime.now(UTC) - timedelta(days=max(1, days))).date().isoformat()
    sql = """
        SELECT kind, name, caller,
               SUM(calls) AS calls,
               MAX(last_ts) AS last_ts,
               MAX(day) AS last_day
        FROM api_usage_daily
        WHERE day >= ?
    """
    params: list = [since]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " GROUP BY kind, name, caller ORDER BY calls DESC, name, caller"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def prune_usage(conn: sqlite3.Connection, keep_days: int = 400) -> int:
    """删除超过保留期（默认 400 天）的日行；返回删除行数。"""
    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).date().isoformat()
    cur = conn.execute("DELETE FROM api_usage_daily WHERE day < ?", (cutoff,))
    conn.commit()
    return cur.rowcount or 0
