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

    # P1-9（2026-11-04）：成本告警档位（只读；无账本时 available=False）
    from sgme.engine import cost_alert

    budget = cost_alert.budget_status(mem_conn, request.app.state.cfg)

    if not _table_exists(mem_conn, "llm_usage_daily"):
        return {"items": [], "totals": empty_total, "days": span,
                "budget": budget, "note": "用量账本尚未创建（无记账）"}

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

    return {"items": items, "totals": totals, "days": span, "budget": budget}


@router.get("/v1/admin/ops/refine-efficiency")
def refine_efficiency(
    request: Request,
    days: str | None = None,
    _: str = Depends(require_admin_key),
):
    """块级提炼成本效率（D-80，2026-11-06）：缓存命中 / 同块重复率 / 补跑积压。

    数据源：refine_runs（chunk_sha / cache_hit / reused_tokens）+ refine_chunk_failures。

    判读口径：
    - repeat_ratio = 总批次 / 不同 (file_id, chunk_sha) 组合数。≈1 表示每块只算一次；
      >1.5 说明仍在重复重发（缓存没命中或被关掉），是成本异常的第一信号。
    - cache_hit_rate = 命中批次 / 总批次。稳态 10%~40% 属正常（新内容总要跑一遍）。
    - replay_pending 长期不降 = 补跑队列堵住，看 last_error。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    span = _parse_days(days)
    since = f"-{span} days"
    out = {
        "days": span,
        "runs": 0,
        "chunked_runs": 0,
        "distinct_chunks": 0,
        "repeat_ratio": 0.0,
        "cache_hits": 0,
        "cache_hit_rate": 0.0,
        "reused_tokens": 0,
        "replay_pending": 0,
        "available": True,
    }
    if not _table_exists(mem_conn, "refine_runs"):
        out["available"] = False
        out["note"] = "refine_runs 尚未创建"
        return out
    row = mem_conn.execute(
        """
        SELECT COUNT(*),
               COUNT(DISTINCT file_id || '|' || chunk_sha),
               COALESCE(SUM(CASE WHEN cache_hit=1 THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(reused_tokens), 0),
               COALESCE(SUM(CASE WHEN chunk_sha IS NOT NULL THEN 1 ELSE 0 END), 0)
        FROM refine_runs
        WHERE julianday(started_at) >= julianday('now', ?)
        """,
        (since,),
    ).fetchone()
    if row:
        runs = int(row[0] or 0)
        distinct = int(row[1] or 0)
        hits = int(row[2] or 0)
        # 重复率只统计「有块指纹」的批次：D-80 上线前的历史行 chunk_sha 全是 NULL，
        # 若把它们算进来，所有 NULL 会塌成同一个 distinct 键 → 比例虚高（实测刚上线
        # 就报 16.3，误导运维）。上线后新批次才是有意义的分母。
        chunked = int(row[4] or 0)
        out["runs"] = runs
        out["chunked_runs"] = chunked
        out["distinct_chunks"] = distinct
        out["repeat_ratio"] = round(chunked / distinct, 3) if distinct else 0.0
        out["cache_hits"] = hits
        out["cache_hit_rate"] = round(hits / chunked, 4) if chunked else 0.0
        out["reused_tokens"] = int(row[3] or 0)
    try:
        from sgme.data import chunk_failure_dao

        out["replay_pending"] = chunk_failure_dao.count(mem_conn)
    except Exception:  # noqa: BLE001——队列未建表时按 0 处理
        pass
    return out

@router.get("/v1/admin/ops/health-overview")
def health_overview(
    request: Request,
    _: str = Depends(require_admin_key),
):
    """运维健康总览：存储体检 + 9.6 场景下线就绪（只读，B300）。"""
    from sgme.operations.ops_health import health_overview as _health_overview

    return _health_overview(request.app.state.cfg)
