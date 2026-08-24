# -*- coding: utf-8 -*-
"""sgme/server/routes_persona.py：人格洞察 HTTP 端点（ST-35 T-101）。

扩展模块路由：persona.enabled=true 时由 server/app.py 挂载（与 care 同模式）。
鉴权：require_agent_key（用户级资产，与 care 同权限级）。
业务逻辑全部下沉 data/persona_dao + engine/persona_monthly，本文件只做协议翻译。

端点：
- GET  /v1/admin/persona/traits          特质列表（dimension/status/min_confidence 过滤）
- GET  /v1/admin/persona/mbti            MBTI 轨迹 + 最新值
- POST /v1/admin/persona/mbti            追加自报 MBTI 记录
- GET  /v1/admin/persona/reports         月报列表
- GET  /v1/admin/persona/reports/{id}    月报详情
- POST /v1/admin/persona/calibrate       手动触发月度校准（LLM 调用）
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from sgme.data import persona_dao
from sgme.server.app import require_agent_key

router = APIRouter()


class MbtiAddRequest(BaseModel):
    mbti_type: str
    note: str | None = None


def _mem(request: Request) -> sqlite3.Connection:
    return request.app.state.mem_conn


# ---------- GET /v1/admin/persona/traits ----------

@router.get("/v1/admin/persona/traits")
def list_traits(
    request: Request,
    dimension: str | None = None,
    status: str = "active",
    min_confidence: float | None = None,
    _: str = Depends(require_agent_key),
):
    traits = persona_dao.list_traits(
        _mem(request),
        dimension=dimension,
        status=status,
        min_confidence=min_confidence,
        limit=200,
    )
    return {"traits": traits, "count": len(traits)}


# ---------- GET /v1/admin/persona/mbti ----------

@router.get("/v1/admin/persona/mbti")
def mbti_history(
    request: Request,
    _: str = Depends(require_agent_key),
):
    mem = _mem(request)
    return {
        "history": persona_dao.get_mbti_history(mem),
        "latest": persona_dao.get_latest_mbti(mem),
    }


# ---------- POST /v1/admin/persona/mbti ----------

@router.post("/v1/admin/persona/mbti")
def add_mbti(
    req: MbtiAddRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    try:
        record = persona_dao.add_mbti_record(_mem(request), req.mbti_type, note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"ERR_INVALID_ARGS: {e}")
    return {"record": record}


# ---------- GET /v1/admin/persona/reports ----------

@router.get("/v1/admin/persona/reports")
def list_reports(
    request: Request,
    limit: int = 12,
    _: str = Depends(require_agent_key),
):
    reports = persona_dao.list_reports(_mem(request), limit=limit)
    return {"reports": reports, "count": len(reports)}


# ---------- GET /v1/admin/persona/reports/{report_id} ----------

@router.get("/v1/admin/persona/reports/{report_id}")
def get_report(
    report_id: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    report = persona_dao.get_report(_mem(request), report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"ERR_NOT_FOUND: 报告不存在 {report_id}")
    return {"report": report}


# ---------- POST /v1/admin/persona/calibrate ----------

@router.post("/v1/admin/persona/calibrate")
def trigger_calibration(
    request: Request,
    _: str = Depends(require_agent_key),
):
    """手动触发月度校准（同步阻塞，含 LLM 调用；执行中 → 409）。"""
    from sgme.engine import persona_monthly
    result = persona_monthly.run_calibration(request.app.state.mem_conn, request.app.state.cfg)
    if result.get("status") == "running":
        raise HTTPException(status_code=409, detail="ERR_CONFLICT: 校准执行中")
    return {"result": result}
