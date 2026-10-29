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
- POST   /v1/admin/roles/{role_id}/persona-fill  人设懒补全与写回（D-76）
- POST   /v1/admin/roles/{role_id}/guard-reply   越界三级后果（D-77）
- GET    /v1/admin/roles/{role_id}/metrics       真实感指标快照读取（D-74）
- POST   /v1/admin/roles/{role_id}/metrics       真实感指标记录（D-74，服务端重算）

v0.1：业务逻辑全部下沉 ``sgme.operations.care``，本函数只做协议翻译。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from sgme.operations import care as care_operation
from sgme.operations import role_metrics as role_metrics_operation
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
    render: bool = False,
    user_name: str | None = None,
    _: str = Depends(require_agent_key),
):
    """角色沟通提示词装配（换皮不换芯）：system_prompt + persona + 画像 + 关怀策略。

    inject_mode 可选（daily/full 等模板名）：带上则附加用户画像块（零物化）。
    render=true 时替换 ``{{char}}``/``{{user}}`` 宏（user_name 缺省「你」）——
    适配器历史上都没做这层拼接，导致宏直接进模型、用户可见「早安，{{user}}。」。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    cfg = request.app.state.cfg
    return run_operation(
        care_operation.assemble, role_id, mem_conn, cfg,
        inject_mode=inject_mode, render=render, user_name=user_name,
    )


# ---------- POST /v1/admin/roles/{role_id}/persona-fill（D-76 / T-306） ----------

class PersonaFillRequest(BaseModel):
    """人设懒补全请求体：用户的追问原文。"""

    question: str


@router.post("/v1/admin/roles/{role_id}/persona-fill")
def persona_fill(
    role_id: str,
    payload: PersonaFillRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """人设懒补全与写回（D-76）：查档案 → 命中直接用 → 未命中生成+冲突检查+落盘。

    status：hit（档案命中）/ filled（已生成并写回 character_book）/
    unset（冲突补不了 → 「这个我没提过」，不写回）。
    角色人生事实**只写角色卡，永不写记忆池**（隔离铁律）。
    角色不存在 → ERR_NOT_FOUND；LLM 不可用 → ERR_INTERNAL。
    """
    cfg = request.app.state.cfg
    return run_operation(care_operation.persona_fill, role_id, payload.question, cfg)


# ---------- POST /v1/admin/roles/{role_id}/guard-reply（D-77 / T-307） ----------

class GuardReplyRequest(BaseModel):
    """越界自检请求体：agent 待发的回复原文 + 会话标识（用于同会话阈值）。"""

    reply: str
    session_id: str = ""


@router.post("/v1/admin/roles/{role_id}/guard-reply")
def guard_reply(
    role_id: str,
    payload: GuardReplyRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """越界三级后果（D-77）：软拦截改写一次 → 记录角色卡 → 阈值约束/告警。

    status：clean（未命中，原样返回）/ rewritten（命中并已改写）。
    constraint 非空时，agent 应把该约束附加到本会话后续回复的上下文。
    越界只写角色卡 extensions.sgme_violations，**不进记忆池**。
    """
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        care_operation.guard_reply, role_id, payload.reply, cfg,
        session_id=payload.session_id, mem_conn=mem_conn,
    )


# ---------- GET/POST /v1/admin/roles/{role_id}/metrics（D-74 / T-308） ----------

class MetricsItem(BaseModel):
    """单题测量条目：问题 + 回复原文；like_human 为外部盲读裁判分（可选）。"""

    q: str = ""
    reply: str
    like_human: int | None = None


class MetricsRecordRequest(BaseModel):
    """真实感指标记录请求体。

    四项确定性指标（首句长度/感叹号率/复述率/AI 味句式率）由服务端重算，
    不信任上传的派生数字；盲读类（like_human / blind_misjudge_rate）由外部写入。
    """

    items: list[MetricsItem]
    label: str = ""                     # 快照标签（如 baseline-20261030 / v2）
    blind_misjudge_rate: float | None = None  # 真人盲读误判率（0-1，外部写入）
    set_as_baseline: bool = False       # 尚无 baseline 时固化为基线快照（已有不覆盖）
    meta: dict[str, Any] | None = None  # 运行元信息（variant/provider/script 等）


@router.get("/v1/admin/roles/{role_id}/metrics")
def get_metrics(
    role_id: str,
    _: str = Depends(require_agent_key),
):
    """真实感指标快照读取（D-74）：baseline / latest（含逐题 items）/ runs 历史
    + 越界只读聚合（total / 今日 / by_kind，与 D-77 同表看）。

    快照落角色卡 extensions.sgme_metrics（不新增表）；角色不存在 → ERR_NOT_FOUND。
    """
    return run_operation(role_metrics_operation.metrics_get, role_id)


@router.post("/v1/admin/roles/{role_id}/metrics")
def record_metrics(
    role_id: str,
    payload: MetricsRecordRequest,
    _: str = Depends(require_agent_key),
):
    """真实感指标记录（D-74）：服务端重算四项指标 → 聚合 → 写角色卡。

    baseline 只设一次（``set_as_baseline=True`` 且尚无 baseline 才固化）；
    latest 每次覆盖；runs 最多留 20 条。只写角色卡，**不进记忆池**。
    """
    return run_operation(
        role_metrics_operation.metrics_record, role_id,
        [it.model_dump() for it in payload.items],
        label=payload.label,
        blind_misjudge_rate=payload.blind_misjudge_rate,
        set_as_baseline=payload.set_as_baseline,
        meta=payload.meta,
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
