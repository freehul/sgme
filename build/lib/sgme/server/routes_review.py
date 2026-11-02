"""server/routes_review.py：待审记忆队列端点（D-30 / T-318 后端）。

业务实现全部在 ``sgme.operations.review``，本文件只做协议翻译
（分层约定：入口层只取依赖 → 调 operation → run_operation 统一翻错误码）。

端点：
    GET  /v1/admin/review/pending            待审列表（needs_review=1）
    POST /v1/admin/review/{memory_id}/approve 放行（needs_review=0）
    POST /v1/admin/review/{memory_id}/reject  拒绝（status='rejected'）
    POST /v1/admin/review/auto-resolve       手工触发超期自动消化（D-30 七天规则）

鉴权：全部 Depends(require_agent_key)——与 memory_reject 同档（require_agent_key 同时接受
agent key 与 admin key，故 WebUI 用 admin key 亦可）。待审放行/拒绝属用户裁决，
agent 代用户操作时同样走这里。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from sgme.operations.review import (
    approve as approve_operation,
    auto_resolve_expired as auto_resolve_operation,
    list_pending as list_pending_operation,
    reject as reject_operation,
)
from sgme.server.app import require_agent_key, run_operation

router = APIRouter()


@router.get("/v1/admin/review/pending")
def list_pending(
    request: Request,
    limit: str | None = None,
    offset: str | None = None,
    dimension: str | None = None,
    _: str = Depends(require_agent_key),
):
    """待审列表（只读）。

    参数一律以 ``str | None`` 收入，解析/校验下沉 operations 层
    （越界或非整数 → 400 ERR_INVALID_ARGS）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    # HTTP query 一律是 str，operations 层要 int——在此转换（非法值回退默认，
    # 不把 "3" 这类字符串直接喂给 list_pending 的 _parse_limit，否则 400）
    def _int_or(raw: str | None, default: int) -> int:
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    return run_operation(
        list_pending_operation,
        mem_conn,
        limit=_int_or(limit, 50),
        offset=_int_or(offset, 0),
        dimension=dimension,
    )


@router.post("/v1/admin/review/{memory_id}/approve")
def approve(
    request: Request,
    memory_id: str,
    body: dict | None = None,
    _: str = Depends(require_agent_key),
):
    """放行一条待审记忆（幂等）。

    Body：``note``（可选，追加到 memories.notes）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    note = (body or {}).get("note")
    return run_operation(approve_operation, mem_conn, memory_id, note=note)


@router.post("/v1/admin/review/{memory_id}/reject")
def reject(
    request: Request,
    memory_id: str,
    body: dict | None = None,
    _: str = Depends(require_agent_key),
):
    """拒绝一条待审记忆（幂等，软删可恢复）。

    Body：``reason``（拒绝理由，写入 reject_reason）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    reason = (body or {}).get("reason") or ""
    return run_operation(reject_operation, mem_conn, memory_id, reason=reason)


@router.post("/v1/admin/review/auto-resolve")
def auto_resolve(
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_agent_key),
):
    """手工触发超期自动消化（D-30：7 天未处理按保守直存消化）。

    Body：``days``（可选，默认 7）。梦境/定时任务也可调用。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    raw_days = (body or {}).get("days", 7)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = 7
    return run_operation(auto_resolve_operation, mem_conn, days=days)
