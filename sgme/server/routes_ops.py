"""server/routes_ops.py：运维可观测性端点（SGME 2.0 运维工作面后端）。

两个看板的数据源，均只读、均对「表还没建/还没数据」优雅降级：

  GET /v1/admin/ops/ttl-disposition?days=30
      TTL 处置审计（D-52 五档：valid_expired / renewed / anchor / sleeping / archived）。
      数据源 ``ttl_disposition``（recall_aging.ensure_ttl_disposition_table 懒建）。

  GET /v1/admin/ops/llm-cost?days=30
      LLM 用量账本（设计文档第 12 项 / D-63）。
      数据源 ``llm_usage_daily``（llm_usage_dao.ensure_table 懒建）。

表不存在时返回空 items + 零 total，而不是 404/500——看板要能显示「暂无数据」。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from sgme.server.app import require_admin_key

router = APIRouter()

# D-52 五档处置的固定枚举（顺序即看板展示顺序）
TTL_DISPOSITIONS = ("valid_expired", "renewed", "anchor", "sleeping", "archived")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _parse_days(raw: str | None, default: int = 30) -> int:
    try:
        days = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return days if 1 <= days <= 365 else default


@router.get("/v1/admin/ops/ttl-disposition")
def ttl_disposition(
    request: Request,
    days: str | None = None,
    _: str = Depends(require_admin_key),
):
    """TTL 处置按天统计（看板用）。表缺失或空数据一律返回空集。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    span = _parse_days(days)
    empty_total = {"valid_expired": 0, "renewed": 0, "anchor": 0, "sleeping": 0, "archived": 0}

    if not _table_exists(mem_conn, "ttl_disposition"):
        return {"items": [], "total": empty_total, "days": span, "note": "审计表尚未创建（无处置记录）"}

    rows = mem_conn.execute(
        """
        SELECT substr(created_at, 1, 10) AS day, disposition, COUNT(*) AS n
        FROM ttl_disposition
        WHERE julianday(created_at) >= julianday('now', ?)
        GROUP BY day, disposition
        ORDER BY day DESC
        """,
        (f"-{span} days",),
    ).fetchall()

    by_day: dict[str, dict[str, int]] = {}
    total = dict(empty_total)
    for day, disposition, n in rows:
        bucket = by_day.setdefault(str(day), dict(empty_total))
        if disposition in bucket:
            bucket[disposition] = int(n)
        if disposition in total:
            total[disposition] += int(n)

    items = [{"day": day, **counts} for day, counts in sorted(by_day.items(), reverse=True)]
    return {"items": items, "total": total, "days": span}


@router.get("/v1/admin/ops/llm-cost")
def llm_cost(
    request: Request,
    days: str | None = None,
    _: str = Depends(require_admin_key),
):
    """LLM 用量按 (day, provider, model, stage) 统计（看板用）。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    span = _parse_days(days)
    empty_total = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    if not _table_exists(mem_conn, "llm_usage_daily"):
        return {"items": [], "totals": empty_total, "days": span, "note": "用量账本尚未创建（无记账）"}

    rows = mem_conn.execute(
        """
        SELECT day, provider, model, stage, prompt_tokens, completion_tokens, calls
        FROM llm_usage_daily
        WHERE julianday(day) >= julianday('now', ?)
        ORDER BY day DESC, prompt_tokens DESC
        """,
        (f"-{span} days",),
    ).fetchall()

    items = []
    totals = dict(empty_total)
    for day, provider, model, stage, pt, ct, calls in rows:
        pt, ct, calls = int(pt or 0), int(ct or 0), int(calls or 0)
        items.append(
            {
                "day": day,
                "provider": provider,
                "model": model,
                "stage": stage,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "calls": calls,
            }
        )
        totals["prompt_tokens"] += pt
        totals["completion_tokens"] += ct
        totals["calls"] += calls

    return {"items": items, "totals": totals, "days": span}
