# -*- coding: utf-8 -*-
"""sgme/server/routes_care.py：Care Engine 角色层 HTTP 端点（ST-25 / T-35）。

扩展模块路由：care.enabled=true 时由 server/app.py 挂载。
鉴权：读/写均 require_agent_key（与记忆同权限级——角色卡是用户级资产）。

端点：
- GET    /v1/admin/roles                 角色卡列表（轻量）
- GET    /v1/admin/roles/{role_id}       单张角色卡全文
- POST   /v1/admin/roles/{role_id}       新建/更新角色卡（幂等 upsert）
- DELETE /v1/admin/roles/{role_id}       归档（移入 .archive/，原件永不删）
- GET    /v1/admin/roles/{role_id}/persona       读取 persona 物化文件
- POST   /v1/admin/roles/{role_id}/persona       生成 persona（LLM 四层扫描）

v0.1：业务逻辑全部下沉 ``sgme.operations.care``，本函数只做协议翻译。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from sgme.operations import care as care_operation
from sgme.server.app import require_agent_key, run_operation

router = APIRouter()


class RoleUpsertRequest(BaseModel):
    """角色卡写入请求：data 段（CC V2 兼容子集，name/description 必填）。"""

    data: dict[str, Any]


# ---------- GET /v1/admin/roles ----------

@router.get("/v1/admin/roles")
def list_roles(
    _: str = Depends(require_agent_key),
):
    """角色卡列表（role_id/name/description/updated_at 轻量字段）。"""
    return run_operation(care_operation.list_roles)


# ---------- GET /v1/admin/roles/{role_id} ----------

@router.get("/v1/admin/roles/{role_id}")
def get_role(
    role_id: str,
    _: str = Depends(require_agent_key),
):
    """单张角色卡全文；不存在 → ERR_NOT_FOUND。"""
    return run_operation(care_operation.get_role, role_id)


# ---------- POST /v1/admin/roles/{role_id} ----------

@router.post("/v1/admin/roles/{role_id}")
def upsert_role(
    role_id: str,
    payload: RoleUpsertRequest,
    _: str = Depends(require_agent_key),
):
    """新建/更新角色卡（幂等 upsert，校验失败 → ERR_INVALID_ARGS）。"""
    return run_operation(care_operation.create_role, role_id, payload.data)


# ---------- DELETE /v1/admin/roles/{role_id} ----------

@router.delete("/v1/admin/roles/{role_id}")
def delete_role(
    role_id: str,
    _: str = Depends(require_agent_key),
):
    """归档角色卡（移入 .archive/，原件永不删；不存在 → ERR_NOT_FOUND）。"""
    return run_operation(care_operation.delete_role, role_id)


# ---------- GET /v1/admin/roles/{role_id}/persona ----------

@router.get("/v1/admin/roles/{role_id}/persona")
def get_persona(
    role_id: str,
    _: str = Depends(require_agent_key),
):
    """读取 persona 物化文件；未生成 → ERR_NOT_FOUND。"""
    return run_operation(care_operation.get_persona, role_id)


# ---------- POST /v1/admin/roles/{role_id}/persona ----------

@router.post("/v1/admin/roles/{role_id}/persona")
def generate_persona(
    role_id: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """生成角色 persona（LLM 四层扫描 + 记忆池画像素材）并物化。

    LLM 不可用 → ERR_INTERNAL（persona 无降级语义，不降级直存）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    cfg = request.app.state.cfg
    return run_operation(
        care_operation.generate_persona, role_id, mem_conn, cfg,
    )


# ---------- GET /v1/admin/roles/{role_id}/assemble ----------

@router.get("/v1/admin/roles/{role_id}/assemble")
def assemble_role(
    role_id: str,
    request: Request,
    inject_mode: str | None = None,
    _: str = Depends(require_agent_key),
):
    """角色沟通提示词装配（换皮不换芯）：system_prompt + persona + 画像 + 关怀策略。

    inject_mode 可选（daily/full 等模板名）：带上则附加用户画像块（零物化）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    cfg = request.app.state.cfg
    return run_operation(
        care_operation.assemble, role_id, mem_conn, cfg,
        inject_mode=inject_mode,
    )


# ---------- 关怀信号（T-36） ----------

class ActiveRoleRequest(BaseModel):
    """设置当前角色请求体。"""

    role_id: str


# ---------- 当前角色（T-40） ----------

@router.get("/v1/admin/care/active-role")
def get_active_role(
    _: str = Depends(require_agent_key),
):
    """读取当前沟通角色；未设置 → role_id=None。"""
    return run_operation(care_operation.get_active_role)


@router.put("/v1/admin/care/active-role")
def set_active_role(
    payload: ActiveRoleRequest,
    _: str = Depends(require_agent_key),
):
    """设置当前沟通角色（换皮不换芯；角色不存在 → ERR_NOT_FOUND）。"""
    return run_operation(care_operation.set_active_role, payload.role_id)


# ---------- POST /v1/admin/care/scan ----------

@router.post("/v1/admin/care/scan")
def scan_care_signals(
    request: Request,
    _: str = Depends(require_agent_key),
):
    """触发关怀信号扫描（待办到期/情绪/过劳/每日，幂等去重，零 LLM）。

    消费方（agent）可定时调用；SGME 只发信号不做决策。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    cfg = request.app.state.cfg
    return run_operation(care_operation.scan_signals, mem_conn, cfg)


# ---------- GET /v1/admin/care/signals ----------

@router.get("/v1/admin/care/signals")
def list_care_signals(
    request: Request,
    signal_type: str | None = None,
    unconsumed_only: bool = False,
    limit: int = 50,
    _: str = Depends(require_agent_key),
):
    """拉取关怀信号（type=care_*；unconsumed_only=true 只看未消费）。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        care_operation.list_signals, mem_conn,
        signal_type=signal_type, unconsumed_only=unconsumed_only, limit=limit,
    )


# ---------- POST /v1/admin/care/signals/{event_id}/consume ----------

@router.post("/v1/admin/care/signals/{event_id}/consume")
def consume_care_signal(
    event_id: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """原子认领关怀信号（谁消费谁标记，ST-27 T-57）。

    agent_id 从鉴权 key 反查；已被他人消费 → 409 ERR_CONFLICT。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    agent_id = request.app.state.key_store.resolve_agent_id(request.headers.get("X-API-Key"))
    return run_operation(care_operation.consume_signal, mem_conn, event_id, agent_id)


# ---------- POST /v1/admin/care/signals/{event_id}/ack ----------

class SignalAckRequest(BaseModel):
    """信号消费回执请求体（ST-27 T-57）。"""

    status: str          # claimed / acked / failed
    result: str | None = None  # 处理结果摘要（如「已转告用户」「health 检查正常」）


@router.post("/v1/admin/care/signals/{event_id}/ack")
def ack_care_signal(
    event_id: str,
    payload: SignalAckRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """写消费回执（signal_acks 表）：claimed/acked/failed，ST-27 T-57。

    agent_id 从鉴权 key 反查；幂等 upsert（同 agent 重复回执覆盖最新状态）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    agent_id = request.app.state.key_store.resolve_agent_id(request.headers.get("X-API-Key"))
    return run_operation(
        care_operation.ack_signal, mem_conn, event_id, agent_id, payload.status, payload.result,
    )
