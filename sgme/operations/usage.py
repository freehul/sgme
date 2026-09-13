"""operations/usage.py：接口调用统计操作（T-163，2026-09-13）。

三段式（照抄 stats.py 样板；本模块响应即最终形态，单入口无需协议投影）。

承接的入口
----------
- HTTP 中间件（server/app.py ``UsageMiddleware``）/ MCP 中间件
  （mcp_server.py ``ApiKeyMiddleware``）→ ``record_usage``
- HTTP ``GET /v1/admin/usage``（管理员 Key）→ ``query_usage``

依赖：SQL 一律走 ``sgme.data.usage_dao``（data 层唯一出口）；本层只做参数
校验与响应组装。``record_usage`` 的"全静默"由调用方（中间件）兜底——
统计是旁路，任何失败不得影响请求。
"""

from __future__ import annotations

import sqlite3

from sgme.data import usage_dao
from sgme.operations.errors import OperationResult

KINDS = ("http", "mcp")
MAX_DAYS = 400
DEFAULT_DAYS = 30


def record_usage(
    conn: sqlite3.Connection,
    kind: str,
    name: str,
    caller: str,
    ip: str | None = None,
) -> None:
    """记录一次调用（name=路由模板或工具名；异常由调用方静默兜底）。"""
    usage_dao.record_usage(conn, kind, name, caller, ip)


def query_usage(
    conn: sqlite3.Connection,
    days: int = DEFAULT_DAYS,
    kind: str | None = None,
) -> OperationResult:
    """近 N 天调用统计：按「端点/工具 × 调用方」聚合（次数降序）。"""
    try:
        days_v = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    except (TypeError, ValueError):
        return OperationResult.fail(
            "ERR_INVALID_ARGS", f"days 需为整数（1~{MAX_DAYS}）"
        )
    if kind not in (None, *KINDS):
        return OperationResult.fail("ERR_INVALID_ARGS", "kind 仅支持 http / mcp")
    items = usage_dao.query_usage(conn, days=days_v, kind=kind)
    return OperationResult.succeed(
        {
            "days": days_v,
            "kind": kind,
            "total_rows": len(items),
            "items": items,
        }
    )
