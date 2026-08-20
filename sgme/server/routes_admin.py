"""server/routes_admin.py：Admin 端点（管理员 Key）。

- POST   /v1/admin/agents/register   签发 Agent Key
- DELETE /v1/admin/agents/{agent_id} 吊销 Agent Key
- GET    /v1/admin/agents            只读列出已注册 Agent（脱敏，SCSM 镜像同步用）
- GET    /v1/admin/stats             统计
- POST   /v1/admin/refine/trigger    手动触发提炼（batch / 指定文件）
- POST   /v1/admin/tier0/refresh     手动触发 Tier0 摘要生成
- GET    /v1/admin/memories          记忆分页列表（0.8 T-15 / 契约 §5.3）
- GET    /v1/admin/scenes            场景分页列表（0.8 T-15 / 契约 §5.4）
- GET    /v1/admin/refine_runs       提炼记录分页（0.8 T-15 / 契约 §5.5）
- GET    /v1/admin/sessions          L0 会话列表（0.8 T-15 / 契约 §5.6.1）
- GET    /v1/admin/sessions/{file_id} L0 会话原文（0.8 T-15 / 契约 §5.6.2）
- GET    /v1/admin/stats/detail      token 成本/质量明细（0.8 T-15 / 契约 §5.7）
- GET    /v1/admin/templates         模板列表（§5.8.1，limit/offset 分页）
- POST   /v1/admin/templates         新建模板（§5.8.3，重名 409）
- PUT    /v1/admin/templates/{name}  更新模板（§5.8.2，校验失败 400）
- DELETE /v1/admin/templates/{name}  删除模板（§5.8.4，内置 400 / 不存在 404）

提炼链路：refine_file (L0→L1) → l15.resolve_conflicts (L1.5 落库)。
L1.5 LLM 不可用时降级直存（保守不丢数据）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sgme.operations.browse import get_session_raw as get_session_raw_operation
from sgme.operations.browse import list_memories as list_memories_operation
from sgme.operations.graph import get_graph as get_graph_operation
from sgme.operations.browse import list_refine_runs as list_refine_runs_operation
from sgme.operations.browse import list_sessions as list_sessions_operation
from sgme.operations.browse import stats_detail as stats_detail_operation
from sgme.operations.scene import list_scenes as list_scenes_operation
from sgme.operations.template import create_template as create_template_operation
from sgme.operations.template import delete_template as delete_template_operation
from sgme.operations.template import list_templates as list_templates_operation
from sgme.operations.template import update_template as update_template_operation
from sgme.engine import dream as dream_mod

# 规范：operations 一律走完整子模块路径导入（包级不扁平导出操作函数）
# ——详见 operations/__init__.py「导入规范」。
from sgme.operations.stats import http_payload as stats_http_payload
from sgme.operations.stats import stats as stats_operation
from sgme.operations.refine import refine_trigger as refine_trigger_operation
from sgme.operations.refine import refine_trigger_async as refine_trigger_async_operation
from sgme.profile import tier0 as tier0_mod
from sgme.server.app import _now_iso, api_error, require_admin_key, run_operation
from sgme.data import stats_dao

logger = logging.getLogger("sgme.server.admin")

router = APIRouter()


# ---------- 请求模型 ----------

class RegisterAgentRequest(BaseModel):
    agent_id: str
    scope: list[str] | None = None
    agent_model: str | None = None  # T-43：声明的提炼模型（provider/model），提炼动态链跟随


class RefineTriggerRequest(BaseModel):
    file_id: str | None = None
    limit: int = 100


class Tier0RefreshRequest(BaseModel):
    pass  # 无参数，触发即可


# ---------- POST /v1/admin/agents/register ----------

@router.post("/v1/admin/agents/register")
def register_agent(
    payload: RegisterAgentRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """签发新 Agent API Key。"""
    store = request.app.state.key_store
    if store.agent_exists(payload.agent_id):
        raise api_error(
            "ERR_CONFLICT",
            f"Agent 已存在: {payload.agent_id}（如需新 Key 请先吊销后重签）",
        )
    key = store.register_agent(agent_id=payload.agent_id, scope=payload.scope,
                               agent_model=payload.agent_model)
    return {
        "agent_id": payload.agent_id,
        "api_key": key,
        "role": "agent",
        "scope": payload.scope or [],
        "agent_model": payload.agent_model or "",
        "note": "密钥仅此一次返回，请妥善保存",
    }


@router.delete("/v1/admin/agents/{agent_id}")
def revoke_agent(
    agent_id: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """吊销 Agent Key（§6：删除即失效，随时吊销）。"""
    store = request.app.state.key_store
    if agent_id == "default":
        from sgme.server.app import api_error
        raise api_error("ERR_INVALID_ARGS", "default（env 主 key）不可吊销，请改环境变量")
    revoked = store.revoke_agent(agent_id)
    if revoked == 0:
        from sgme.server.app import api_error
        raise api_error("ERR_NOT_FOUND", f"Agent 不存在或已吊销: {agent_id}")
    logger.info("吊销 Agent: %s（%d 个 Key）", agent_id, revoked)
    return {"status": "ok", "agent_id": agent_id, "revoked": revoked}


# ---------- GET /v1/admin/agents ----------

def _last_seen_map(session_conn: sqlite3.Connection) -> dict[str, str]:
    """聚合每个 agent_id 的最后活跃时间。

    SQL：``MAX(COALESCE(ended_at, started_at)) GROUP BY agent_id``。

    ⚠️ 语义硬约定：这是「**该 Agent 最后一次 append 会话的时间**」，
    **不是心跳**。禁止把它文档化或改名为 heartbeat。

    查询与降级逻辑已收编进 stats_dao.agent_last_seen（模块化重构 B30）。
    """
    return stats_dao.agent_last_seen(session_conn)


def _parse_iso(ts: str | None) -> datetime | None:
    """解析 ISO8601 时间戳（容忍 'Z' 后缀与无时区形态）；失败返回 None。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_active_within(raw: str | None) -> int | None:
    """校验并解析 active_within_sec 查询参数。

    Args:
        raw: 原始查询串（FastAPI 传入）。None / 空串表示不过滤。

    Returns:
        非负整数，或 None（不过滤）。

    Raises:
        HTTPException: 非整数或负数 → 400 ERR_INVALID_ARGS。
    """
    if raw is None or raw == "":
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise api_error(
            "ERR_INVALID_ARGS", f"active_within_sec 必须是非负整数: {raw}"
        ) from None
    if val < 0:
        raise api_error("ERR_INVALID_ARGS", f"active_within_sec 必须 ≥ 0: {raw}")
    return val


@router.get("/v1/admin/agents")
def list_agents(
    request: Request,
    role: str | None = None,
    active_within_sec: str | None = None,
    _: str = Depends(require_admin_key),
):
    """只读列出已注册 Agent（脱敏 + 活跃度聚合）。

    供 SCSM `RegistryMirror` 自动同步「谁存在 / 是否活跃」使用，
    替代此前 config 手工维护 Agent 清单的做法。

    纯只读、幂等、无副作用。响应约定见 `SGME-接口契约-v0.1.md` §5.2：
    - 🔴 **绝不含明文 API Key**，只给脱敏指纹 key_ref
    - 过滤合成条目 agent_id="default"
    - `endpoint` 字段位恒为 null（SGME 不掌握 Agent 的回调端点）
    - `status` 恒为 "active"（revoke 是硬删除，无 tombstone）
    - `last_seen_at` 来自 raw_files 聚合，**不是心跳**；无记录为 null

    Args:
        request: FastAPI 请求（取 app.state.key_store / session_conn）。
        role: 可选，精确过滤 role。不匹配返回空列表而非 404。
        active_within_sec: 可选，仅返回 last_seen_at 在 N 秒内的 Agent；
            该参数存在时，last_seen_at=null 的条目会被过滤掉。

    Returns:
        {"agents": [...], "count": int, "generated_at": iso,
         "snapshot_at": iso, "source": "sgme.key_store"}
    """
    within = _parse_active_within(active_within_sec)

    store = request.app.state.key_store
    session_conn: sqlite3.Connection = request.app.state.session_conn

    agents = store.list_agents_public()          # 已聚合 + 已脱敏 + 已过滤 default
    last_seen = _last_seen_map(session_conn)     # 失败降级为 {}
    now = datetime.now(timezone.utc)

    out: list[dict] = []
    for a in agents:
        if role is not None and a.get("role") != role:
            continue
        ts = last_seen.get(a["agent_id"])
        if within is not None:
            dt = _parse_iso(ts)
            if dt is None:
                continue  # 无活跃记录 / 时间戳不可解析 → 在活跃过滤下剔除
            if (now - dt).total_seconds() > within:
                continue
        out.append({
            "agent_id": a["agent_id"],
            "role": a["role"],
            "scope": a["scope"],
            "endpoint": a["endpoint"],          # 恒 null，字段位为将来预留
            "status": a["status"],
            "registered_at": a["registered_at"],
            "last_seen_at": ts,                 # 可为 null
            "last_seen_source": "append" if ts else None,
            "key_count": a["key_count"],
            "key_ref": a["key_ref"],            # 脱敏指纹，非明文
        })

    generated_at = _now_iso()
    return {
        "agents": out,
        "count": len(out),
        "generated_at": generated_at,
        # snapshot_at 与 generated_at 同值，供两版契约命名同时兼容（见 §5.2 注）
        "snapshot_at": generated_at,
        "source": "sgme.key_store",
    }


# ---------- GET /v1/admin/stats ----------

@router.get("/v1/admin/stats")
def admin_stats(
    request: Request,
    _: str = Depends(require_admin_key),
):
    """统计：记忆/原始层计数、维度分布、水位。

    v0.7：业务逻辑已下沉 ``sgme.operations.stats``，本函数只做协议翻译——
    从 app.state 取依赖（含鉴权设施 key_store）→ 调 operation → 投影为
    HTTP 历史契约形态。响应体与 v0.6 逐字段等价（HTTP 与 MCP 的顶层键序、
    refinement 形态、有无 agents 均存在历史差异，故需 http_payload 投影）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn
    # 注册 agents 来自鉴权设施（非数据层），由入口层取出后显式传入 operations
    store = request.app.state.key_store

    data = run_operation(
        stats_operation, mem_conn, session_conn, agents=store.list_agents(),
    )
    return stats_http_payload(data)


# ---------- POST /v1/admin/refine/trigger ----------

@router.post("/v1/admin/refine/trigger")
def refine_trigger(
    payload: RefineTriggerRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """手动触发提炼：指定 file_id 或扫 status=new 批量。

    v0.7：业务逻辑已下沉 ``sgme.operations.refine``，本函数只做协议翻译。
    同步执行（可能耗时数分钟，真实 LLM 分块）。需要立即返回用 /trigger_async。
    """
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn

    return run_operation(
        refine_trigger_operation,
        mem_conn,
        session_conn,
        cfg,
        file_id=payload.file_id,
        limit=payload.limit,
    )


@router.post("/v1/admin/refine/trigger_async")
def refine_trigger_async(
    payload: RefineTriggerRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """异步触发提炼：后台线程执行，立即返回 202。

    v0.7：业务逻辑已下沉 ``sgme.operations.refine``，本函数只做协议翻译。
    后台执行体在 engine/pipeline.async_refine_worker，
    失败由 SGME 批扫兜底（status=new 文件会被下次 trigger 拾起）。
    """
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn

    return run_operation(
        refine_trigger_async_operation,
        mem_conn,
        session_conn,
        cfg,
        file_id=payload.file_id,
        limit=payload.limit,
    )


# ---------- POST /v1/admin/tier0/refresh ----------

@router.post("/v1/admin/tier0/refresh")
def refresh_tier0(
    request: Request,
    _: str = Depends(require_admin_key),
):
    """手动触发 Tier0 摘要生成。"""
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    summary = tier0_mod.generate_summary(mem_conn, cfg)
    if summary:
        tier0_mod.save_summary(summary)
        return {"status": "ok", "summary_length": len(summary)}
    return {"status": "failed", "error": "LLM 不可用或生成失败"}


# ---------- POST /v1/admin/scenes/{scene_id}/status ----------

@router.post("/v1/admin/scenes/{scene_id}/status")
def update_scene_status(
    scene_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """标记场景状态（L2 补丁，2026-08-07）。

    status 枚举（与 memories 一致）：
      active    = 有效，正常参与查询/时间线
      rejected  = 用户判错（错误记忆），不参与查询/时间线
      expired   = 随时间过时（失效），不参与查询/时间线
      archived  = 被合并/替代（L2 merge 内部用）

    场景与记忆的状态语义对齐——L2 场景不会因 L1 记忆 TTL 过期而消失，
    需要显式标记（rejected/expired）或 merge 归档。
    body: {"status": "rejected|expired|active", "reason": "可选说明"}
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    from sgme.data import scene_dao

    scene = scene_dao.get_scene(mem_conn, scene_id)
    if not scene:
        raise api_error("ERR_NOT_FOUND", f"场景不存在: {scene_id}")
    b = body or {}
    status = b.get("status", "active")
    if status not in ("active", "rejected", "expired", "archived"):
        raise api_error("ERR_INVALID_ARGS", f"非法状态: {status}")
    ok = scene_dao.update_scene_status(mem_conn, scene_id, status)
    if not ok:
        raise api_error("ERR_INTERNAL", "状态更新失败")
    return {
        "scene_id": scene_id,
        "status": status,
        "reason": b.get("reason"),
        "note": "rejected/expired 场景不参与查询与时间线，数据保留可溯源",
    }

# ==================== /v1/admin/demands —— 需求池（0.8 ST-15） ====================
#
# 契约文档 §5 未定义需求池端点（属"图纸未覆盖"，经主控裁决按既有 admin 端点范式自拟）。
# 复用惯例：
#   - 鉴权：全部 Depends(require_admin_key)（403 缺失/非管理员，401 Bearer 缺失）
#   - 参数解析：分页/枚举/时间参数走 operations 手工解析（对齐 _parse_active_within 先例），
#     统一回 400 ERR_INVALID_ARGS，而非 FastAPI 默认的 422 校验体
#   - 分页信封：{items, count, total, page, limit, generated_at}（对齐 §5.3/§5.4）
#   - 请求体用裸 dict（对齐本文件 update_scene_status 先例）：错误信封可控，
#     且 PATCH 能区分「显式传 null（解绑）」与「未传该键（保持原值）」
#
# operations / data 依赖采用**函数内导入**（本文件既有先例：update_scene_status 内
# `from sgme.data import scene_dao`），使本段成为文件尾部纯追加，
# 不触碰顶部 import 块——ST-14/ST-16 并行改本文件时冲突面最小。


@router.get("/v1/admin/demands")
def list_demands(
    request: Request,
    page: str | None = None,
    limit: str | None = None,
    status: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    _: str = Depends(require_admin_key),
):
    """需求池分页列表（纯只读）。

    Query：
        page: 页码 ≥1，默认 1。
        limit: 页大小 1-200，默认 50（>200 → 400，不静默截断）。
        status: 逗号分隔多值（pending/planned/partial/done）；缺省不过滤。
        project_id: 精确匹配关联项目。
        q: 标题/内容子串（`%`/`_` 已转义）。
        since / until: ISO8601 闭区间，作用于 sort 时间列（sort=priority 时回落 updated_at）。
        sort: priority / updated_at / created_at，默认 updated_at。
        order: desc / asc，默认 desc。

    Returns:
        {items, count, total, page, limit, generated_at}
    """
    from sgme.operations.demand import list_demands as list_demands_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        list_demands_operation,
        mem_conn,
        page=page,
        limit=limit,
        status=status,
        project_id=project_id,
        q=q,
        since=since,
        until=until,
        sort=sort,
        order=order,
    )


@router.post("/v1/admin/demands")
def create_demand(
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """新建需求（支持从创意升格：传 origin_idea_id）。

    Body：
        title（必填）/ content / status（默认 pending）/ priority（0-100，默认 50）/
        project_id / origin_idea_id / source_ref。

    origin_idea_id 语义：来源创意的 memories.memory_id，**只存不校验**——
    创意侧 `custom_flag='promoted'` 标记归 ST-14，跨模块存在性校验留到集成阶段。
    """
    from sgme.operations.demand import create_demand as create_demand_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(create_demand_operation, mem_conn, body=body)


@router.get("/v1/admin/demands/{demand_id}")
def get_demand(
    demand_id: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """单条需求详情；不存在 → 404 ERR_NOT_FOUND。"""
    from sgme.operations.demand import get_demand as get_demand_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(get_demand_operation, mem_conn, demand_id)


@router.patch("/v1/admin/demands/{demand_id}")
def update_demand(
    demand_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """编辑需求：title / content / priority / project_id / source_ref。

    只改传入字段；`project_id: null` = 解绑项目。
    status 不可经此端点修改（有 resolved_at 联动）→ 400，请用 PUT /status。
    """
    from sgme.operations.demand import update_demand as update_demand_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(update_demand_operation, mem_conn, demand_id, body=body)


@router.put("/v1/admin/demands/{demand_id}/status")
def update_demand_status(
    demand_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """需求状态流转（核心端点）。Body：`{"status": "pending|planned|partial|done"}`。

    - 非法状态值 → 400 ERR_INVALID_ARGS；需求不存在 → 404 ERR_NOT_FOUND
    - 不限制流转方向（含 done→pending 回退），每次调用刷新 updated_at
    - 进入 done 落 resolved_at；转出 done 清空 resolved_at
    - planned 未带 project_id 时不拦截，回 warnings=["planned_without_project"]
    """
    from sgme.operations.demand import set_demand_status as set_demand_status_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(set_demand_status_operation, mem_conn, demand_id, body=body)

# ==================== 0.8 ST-16：项目注册表 /v1/admin/projects ====================
#
# 路径由 `SGME-数据模型设计-v0.1.md` §二 project_meta 定死（`POST /v1/admin/projects`）；
# 契约文档 §5 尚未收录本端点集，参数解析 / 分页信封 / 错误码严格复用 §5.2 agents 与
# §5.3 memories 惯例（page ≥ 1 默认 1；limit 1-200 默认 50；信封
# {items, count, total, page, limit, generated_at}）。
#
# 业务实现在 `sgme.operations.project`，本节只做协议翻译（v0.7 §7 分层）。
# operations 模块采用**函数内导入**：与本文件既有惯例一致（见 revoke_agent /
# update_scene_status），且让本次改动是纯追加，压缩 0.8 并行任务的合并冲突面。


class RegisterProjectRequest(BaseModel):
    """POST /v1/admin/projects 请求体（登记项目，upsert 语义）。

    字段**全部可选**是刻意设计：必填校验交给 operations 层，
    从而返回契约规定的 400 `ERR_INVALID_ARGS`（Pydantic 必填会给 422，
    与 §5.3.4 错误码表不符）。
    """

    project_id: str | None = None
    name: str | None = None
    path: str | None = None
    git_repo: str | None = None
    last_active_at: str | None = None
    milestone: str | None = None


@router.post("/v1/admin/projects")
def register_project(
    payload: RegisterProjectRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """登记项目到项目注册表（upsert：同 project_id 二次登记为更新，不报 409）。

    登记入口：`scripts/project_init.py` 六步之④（立项脚本可重跑，故须幂等）。

    Returns:
        {"project": {...}, "created": bool, "generated_at": iso}
    """
    from sgme.operations.project import register_project as register_project_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        register_project_operation,
        mem_conn,
        project_id=payload.project_id,
        name=payload.name,
        path=payload.path,
        git_repo=payload.git_repo,
        last_active_at=payload.last_active_at,
        milestone=payload.milestone,
    )


@router.get("/v1/admin/projects")
def list_projects(
    request: Request,
    q: str | None = None,
    milestone: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    page: str | None = None,
    limit: str | None = None,
    _: str = Depends(require_admin_key),
):
    """项目注册表分页列表（纯只读）。

    Args:
        q: 名称子串过滤（同时匹配 project_id 与 name）。
        milestone: 里程碑精确过滤。
        sort: last_active_at / updated_at（默认）/ created_at。
        order: desc（默认）/ asc。
        page: 页码，≥ 1，默认 1。
        limit: 页大小，1-200，默认 50；>200 → 400 ERR_INVALID_ARGS。

    Returns:
        {"items": [...], "count": int, "total": int, "page": int,
         "limit": int, "generated_at": iso}

    Note:
        page / limit 声明为 str 而非 int，与本文件 `_parse_active_within` 同惯例——
        由 operations 层统一解析，非法值走 400 ERR_INVALID_ARGS
        （FastAPI 的 int 类型转换失败会给 422，不符契约）。
    """
    from sgme.operations.project import list_projects as list_projects_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        list_projects_operation,
        mem_conn,
        q=q,
        milestone=milestone,
        sort=sort,
        order=order,
        page=page,
        limit=limit,
    )


@router.get("/v1/admin/projects/{project_id}")
def get_project(
    project_id: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """项目注册表单条详情；不存在 → 404 ERR_NOT_FOUND。"""
    from sgme.operations.project import get_project as get_project_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(get_project_operation, mem_conn, project_id)


@router.patch("/v1/admin/projects/{project_id}")
def update_project(
    project_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """更新项目元数据（path / git_repo / milestone / last_active_at / name）。

    PATCH 语义：body 里出现的键才更新；可空列（git_repo / last_active_at /
    milestone）显式传 null 表示清空。`updated_at` 恒刷新。
    未知字段或空 body → 400 ERR_INVALID_ARGS；项目不存在 → 404 ERR_NOT_FOUND。

    body 用 dict 而非 Pydantic 模型：PATCH 必须区分「未提供」与「显式 null」，
    dict 的键存在性天然表达这个语义（惯例同 update_scene_status）。

    Returns:
        {"project": {...}, "updated_fields": [...], "generated_at": iso}
    """
    from sgme.operations.project import update_project as update_project_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(update_project_operation, mem_conn, project_id, body)


# ==================== /v1/admin/templates —— 模板管理（0.8 T-16 / 契约 §5.8） ====================
#
# 业务逻辑在 sgme/operations/template.py，本节四个函数只做协议翻译
# （从 app.state 取 dimensions → 调 operation → run_operation 统一翻错误码）。
# 路由刻意挂在本文件而非新建 routes_templates.py：新文件需要在 app.py 里
# include_router，而 app.py 属受限文件——挂这里可做到 app.py 零改动。
#
# 写操作只落 templates/*.yaml，不入 DB、不动 DDL（契约 §5.8「零改动确认」）。


def _registered_dimensions(request: Request) -> list[dict]:
    """取运行时已注册维度（create_app 启动时已从 DB 回刷 cfg["dimensions"]）。

    传给 validate_template 用于「memory_types / section.dimensions 必须已注册」校验。
    """
    cfg = request.app.state.cfg
    return cfg.get("dimensions") or []


@router.get("/v1/admin/templates")
def list_templates(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(require_admin_key),
):
    """模板列表（§5.8.1）。

    Query limit（默认 50）/ offset（默认 0），对齐 SCSM ``list_templates(limit, offset)``。
    items 含 name/display_name/memory_types/token_budget/sections/content（原始 YAML
    全文，编辑回填用）；外层 count/total/generated_at。

    单个模板文件损坏不会让本端点 500——该条目带 ``valid=false`` + ``error`` 返回，
    保证编辑器仍能拉到 content 并修复。

    非整数 limit/offset 由 FastAPI 参数校验拦为 422；范围非法（limit<1 / offset<0）
    由 operations 层拦为 400 ERR_INVALID_ARGS。
    """
    return run_operation(
        list_templates_operation,
        dimensions=_registered_dimensions(request),
        limit=limit,
        offset=offset,
    )


@router.post("/v1/admin/templates")
def create_template(
    payload: dict,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """新建模板（§5.8.3）。

    Body 同 PUT（``name`` 为新名，或用 ``content`` 提交 YAML 全文）。
    重名 → 409 ERR_CONFLICT。响应 ``{created, name, restart_required}``。
    """
    return run_operation(
        create_template_operation,
        None,  # POST 无路径参数，模板名从 body 推断
        payload,
        dimensions=_registered_dimensions(request),
    )


@router.put("/v1/admin/templates/{name}")
def update_template(
    name: str,
    payload: dict,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """更新模板（§5.8.2）。

    Body 为完整模板 JSON，``name`` 必须与路径一致（不一致 → 400）。
    校验复用 ``profile/template.py::validate_template``，失败 400 ERR_INVALID_ARGS
    且 message 带校验详情。写盘为原子写（临时文件 + os.replace）。

    ``restart_required`` 恒 false：实测 ``load_template`` 每请求读盘、无缓存，
    写盘即热加载生效（依据见 operations/template.py 模块 docstring）。
    """
    return run_operation(
        update_template_operation,
        name,
        payload,
        dimensions=_registered_dimensions(request),
    )


@router.delete("/v1/admin/templates/{name}")
def delete_template(
    name: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """删除模板（§5.8.4）。

    内置 4 模板（daily/coding/work/full）→ 400 ERR_INVALID_ARGS「内置模板不可删」；
    不存在 → 404 ERR_NOT_FOUND；成功 → ``{deleted: true, ...}``。
    """
    return run_operation(delete_template_operation, name)

# ---------- F1 浏览类只读端点（0.8 T-15 / 契约 §5.3~§5.7） ----------
#
# 六个端点共用一套形状：**查询参数一律以 ``str | None`` 收入**，不用 FastAPI 的
# int / bool 自动转换。理由：自动转换失败抛的是 422 + FastAPI 自己的
# ValidationError 结构，而契约 §5.3.4 要求参数非法一律 **400 + 统一 error 信封**。
# 解析与校验因此下沉 operations 层（InvalidArgs → run_operation → 400），
# 与本文件既有的 ``_parse_active_within`` 惯例一致。
#
# 全部纯只读、幂等、无副作用；业务逻辑零落在本层（只做取依赖 → 调 operation）。


@router.get("/v1/admin/memories")
def admin_list_memories(
    request: Request,
    page: str | None = None,
    limit: str | None = None,
    dimension_id: str | None = None,
    dimensions: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    since: str | None = None,
    until: str | None = None,
    ttl_filter: str | None = None,
    _: str = Depends(require_admin_key),
):
    """记忆分页列表（契约 §5.3）。

    默认仅返回 ``status=active``；``limit`` 硬上限 200，超限 400。
    ``dimensions``（2026-08-13）：逗号分隔多维度过滤，OR 语义任一命中入选。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        list_memories_operation,
        mem_conn,
        page=page,
        limit=limit,
        dimension_id=dimension_id,
        dimensions=dimensions,
        status=status,
        sort=sort,
        order=order,
        since=since,
        until=until,
        ttl_filter=ttl_filter,
    )


@router.get("/v1/admin/scenes")
def admin_list_scenes(
    request: Request,
    page: str | None = None,
    limit: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    since: str | None = None,
    until: str | None = None,
    _: str = Depends(require_admin_key),
):
    """场景分页列表（契约 §5.4）。默认仅 active，缺省按 heat 倒序。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        list_scenes_operation,
        mem_conn,
        page=page,
        limit=limit,
        status=status,
        sort=sort,
        order=order,
        since=since,
        until=until,
    )


@router.get("/v1/admin/graph")
def admin_graph(
    request: Request,
    scene_limit: int = Query(200, ge=1, le=1000),
    wiki_limit: int = Query(200, ge=1, le=1000),
    memory_limit: int = Query(3000, ge=1, le=20000),
    _: str = Depends(require_admin_key),
):
    """知识图谱数据（ST-13）：nodes（场景/记忆/wiki 页面）+ links（关联关系）。

    数据源：scene_memories（场景↔记忆）+ wiki_links（wiki 页面↔页面）。
    前端 D3 force 布局消费；scene_limit/wiki_limit 控制规模防超大库撑爆。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    wiki_conn: sqlite3.Connection | None = getattr(
        request.app.state, "wiki_conn", None
    )
    return run_operation(
        get_graph_operation,
        mem_conn,
        wiki_conn,
        scene_limit=scene_limit,
        wiki_limit=wiki_limit,
        memory_limit=memory_limit,
    )


@router.get("/v1/admin/refine_runs")
def admin_list_refine_runs(
    request: Request,
    page: str | None = None,
    limit: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    _: str = Depends(require_admin_key),
):
    """提炼记录分页（契约 §5.5）。

    ⚠️ 与 memories/scenes 不同，本端点**不做 status 缺省过滤**——
    提炼监控必须默认可见 error / running。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        list_refine_runs_operation,
        mem_conn,
        page=page,
        limit=limit,
        stage=stage,
        status=status,
        since=since,
        until=until,
    )


@router.get("/v1/admin/sessions")
def admin_list_sessions(
    request: Request,
    page: str | None = None,
    limit: str | None = None,
    session_key: str | None = None,
    agent_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    _: str = Depends(require_admin_key),
):
    """L0 会话列表（契约 §5.6.1）。``session_key`` 为子串匹配。"""
    session_conn: sqlite3.Connection = request.app.state.session_conn
    return run_operation(
        list_sessions_operation,
        session_conn,
        page=page,
        limit=limit,
        session_key=session_key,
        agent_id=agent_id,
        status=status,
        since=since,
        until=until,
    )


@router.get("/v1/admin/sessions/{file_id}")
def admin_get_session(
    file_id: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """L0 会话原文（契约 §5.6.2，与 §4.7 同构的 Admin Key 版）。

    file_id 不存在或磁盘原文缺失 → 404 ``ERR_NOT_FOUND``。
    """
    session_conn: sqlite3.Connection = request.app.state.session_conn
    return run_operation(get_session_raw_operation, session_conn, file_id)


@router.get("/v1/admin/stats/detail")
def admin_stats_detail(
    request: Request,
    period: str | None = None,
    stage: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    _: str = Depends(require_admin_key),
):
    """token 成本 / 质量明细（契约 §5.7）。

    契约参数名为 ``from``（Python 保留字），故以 ``Query(alias="from")``
    映射到形参 ``from_``。聚合 SQL 在 ``stats_dao.refine_detail``（B30）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        stats_detail_operation,
        mem_conn,
        period=period,
        stage=stage,
        from_ts=from_,
        to_ts=to,
    )

# ---------- POST /v1/admin/skills/sync（0.8 ST-11：skills-hub copy 模式真实同步） ----------

class SkillsSyncRequest(BaseModel):
    """skills 同步请求体（§5 触发：direction 三选一）。"""

    direction: str = "both"


@router.post("/v1/admin/skills/sync")
def skills_sync(
    payload: SkillsSyncRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """触发 skills-hub copy 模式同步（远端权威仓 ↔ 本地 cache 工作区）。

    body: ``{"direction": "from_remote" | "to_remote" | "both"}``（默认 both）。

    - 成功 → 200 + 结果 JSON（方向/状态/新增/修改/删除/冲突报告路径/耗时，
      见 SGME-SkillsHub同步设计-v0.1.md §5）
    - mode≠copy / skills_hub 未启用 / direction 非法 / 配置非法 → 400
    - git 失败（远端不可达、超时、冲突策略中止等）→ 500 + stderr 摘要

    SCSM SkillManager「同步」按钮即消费本端点（SCSM 只消费 API，不碰 git）。
    """
    from sgme.skills_hub import GitSyncError, init as init_skills_hub

    direction = (payload.direction or "both").strip().lower()
    if direction not in ("from_remote", "to_remote", "both"):
        raise api_error(
            "ERR_INVALID_ARGS",
            f"未知同步方向: {payload.direction!r}（可选: from_remote / to_remote / both）",
        )
    try:
        hub = init_skills_hub(request.app.state.cfg)
    except ValueError as e:
        raise api_error("ERR_INVALID_ARGS", f"skills_hub 配置非法: {e}") from e
    if hub is None:
        raise api_error("ERR_INVALID_ARGS", "skills_hub 未启用（enabled=false），同步不可用")
    if hub.mode != "copy":
        raise api_error("ERR_INVALID_ARGS", "map 模式无远端语义，同步仅 copy 模式可用")
    try:
        if direction == "both":
            r1 = hub.sync_from_remote()
            r2 = hub.sync_to_remote()
            result = {
                "direction": "both",
                "status": (
                    "conflict_resolved"
                    if any(x.get("conflict") for x in (r1, r2))
                    else ("ok" if any(x["status"] != "noop" for x in (r1, r2)) else "noop")
                ),
                "warnings": r1.get("warnings", []) + r2.get("warnings", []),
                "duration_ms": r1.get("duration_ms", 0) + r2.get("duration_ms", 0),
                "results": [r1, r2],
            }
        else:
            result = (
                hub.sync_from_remote() if direction == "from_remote" else hub.sync_to_remote()
            )
    except GitSyncError as e:
        raise api_error(
            "ERR_INTERNAL",
            f"skills 同步失败: {e.message}",
            {"stderr": e.stderr_summary, "exit_code": e.exit_code},
        ) from e
    except ValueError as e:
        raise api_error("ERR_INVALID_ARGS", str(e)) from e
    return {"sync": result}


# ---------- GET /v1/admin/skills（技能仓库基础信息 + 列表） ----------

def _get_skills_hub(request: Request):
    """按配置初始化 skills_hub；未启用或异常 → 抛 400 API 错误。"""
    try:
        from sgme.skills_hub import init as init_skills_hub

        hub = init_skills_hub(request.app.state.cfg)
    except ValueError as e:
        raise api_error("ERR_INVALID_ARGS", f"skills_hub 配置非法: {e}") from e
    if hub is None:
        raise api_error("ERR_INVALID_ARGS", "skills_hub 未启用（enabled=false）")
    return hub


@router.get("/v1/admin/skills")
def skills_list(
    request: Request,
    _: str = Depends(require_admin_key),
):
    """列出技能仓库全部技能名（只读，复用 SkillsHub.list_skills）。

    返回：``{"enabled": true, "mode": "map", "path": "...", "total": n,
    "skills": ["name", ...]}``。skills_hub 未启用 → 400。
    """
    hub = _get_skills_hub(request)
    names = hub.list_skills()
    return {
        "enabled": True,
        "mode": hub.mode,
        "path": str(hub.root),
        "total": len(names),
        "skills": names,
    }


@router.get("/v1/admin/skills/{name}")
def skills_get(
    name: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """读取单个技能（SKILL.md 全文）。

    - 成功 → 200 + ``{"name": "...", "content": "..."}``
    - 技能名非法 → 400；技能不存在 → 404
    """
    hub = _get_skills_hub(request)
    try:
        content = hub.get_skill(name)
    except ValueError as e:
        raise api_error("ERR_INVALID_ARGS", str(e)) from e
    if content is None:
        raise api_error("ERR_NOT_FOUND", f"技能不存在: {name}")
    return {"name": name, "content": content}


class SkillWriteRequest(BaseModel):
    """技能写入请求体（SKILL.md 全文）。"""

    content: str


@router.put("/v1/admin/skills/{name}")
def skills_put(
    name: str,
    payload: SkillWriteRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """写入/覆盖技能 SKILL.md（复用 SkillsHub.put_skill）。

    - 成功 → 200 + ``{"name": "...", "path": "..."}``
    - 技能名非法 / 内容为空 → 400
    """
    if not payload.content or not payload.content.strip():
        raise api_error("ERR_INVALID_ARGS", "技能内容不能为空")
    hub = _get_skills_hub(request)
    try:
        path = hub.put_skill(name, payload.content)
    except ValueError as e:
        raise api_error("ERR_INVALID_ARGS", str(e)) from e
    return {"name": name, "path": str(path)}


@router.delete("/v1/admin/skills/{name}")
def skills_delete(
    name: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """删除技能目录（复用 SkillsHub.remove_skill，幂等）。

    - 成功 → 200 + ``{"deleted": true/false}``
    - 技能名非法 / 目录含未预期子目录 → 400
    """
    hub = _get_skills_hub(request)
    try:
        deleted = hub.remove_skill(name)
    except ValueError as e:
        raise api_error("ERR_INVALID_ARGS", str(e)) from e
    return {"deleted": deleted}

# ---------- Dream 夜间整理（0.8 ST-10，SGME-Dream夜间整理设计-v0.1.md §5） ----------

@router.post("/v1/admin/dream/trigger")
def dream_trigger(
    request: Request,
    _: str = Depends(require_admin_key),
):
    """手动触发 Dream 夜间整理（202 异步；执行中重复触发 409 ERR_CONFLICT）。

    后台线程执行四步编排（抽取→判决→生命周期→日报），立即返回 202。
    触发时顺带幂等拉起常驻定时器（ensure_scheduler）——生产 Gateway 首次触发后
    即按 dream.schedule 到点自动执行（ST-10 铁律限制接线点，见 engine/dream.py docstring）。
    """
    cfg = request.app.state.cfg

    dream_mod.ensure_scheduler(
        cfg,
        data_dir=getattr(request.app.state, "data_dir", None),
    )
    if dream_mod.is_running():
        # 防重入：执行中重复触发 → 409（ERROR_CODES 已映射 ERR_CONFLICT）
        raise api_error("ERR_CONFLICT", "Dream 夜间整理正在执行中，请勿重复触发")
    try:
        threading.Thread(
            target=dream_mod.run_dream_safe,
            args=(getattr(request.app.state, "data_dir", None), cfg),
            daemon=True,
            name="sgme-dream-run",
        ).start()
    except Exception as e:
        raise api_error("ERR_INTERNAL", f"Dream 触发失败: {e}") from e
    return JSONResponse(status_code=202, content={
        "triggered": "async",
        "status": "queued",
        "note": "后台线程执行四步编排（抽取→判决→生命周期→日报），结果见 /v1/admin/dream/reports",
    })


@router.get("/v1/admin/dream/reports")
def dream_reports_list(
    request: Request,
    page: int = 1,
    limit: int = 50,
    _: str = Depends(require_admin_key),
):
    """Dream 日报分页列表（date 倒序，page/limit 分页）——供 UI/SCSM 消费。"""
    if page < 1:
        raise api_error("ERR_INVALID_ARGS", "page 必须 ≥ 1")
    if not 1 <= limit <= 200:
        raise api_error("ERR_INVALID_ARGS", "limit 必须在 1..200")
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    rows, total = dream_mod.list_reports(mem_conn, page=page, limit=limit)
    return {"reports": rows, "total": total, "page": page, "limit": limit}


@router.get("/v1/admin/dream/reports/{date}")
def dream_report_detail(
    date: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """单日 Dream 日报内容（DB 行 + MD 正文）。日期不存在 → 404。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    row = dream_mod.get_report(mem_conn, date)
    if row is None:
        raise api_error("ERR_NOT_FOUND", f"Dream 日报不存在: {date}")
    return {"report": row}


# ---------- POST /v1/admin/events/consume_all（T-87：信号批量清空） ----------
#
# 业务实现在 sgme/operations/events.py::events_consume_all（与 pull/stream 同模块），
# 本节只做协议翻译。挂本文件而非 routes_events.py 的原因：
#   - 本端点属管理操作（清空未消费信号），沿用 admin 鉴权 require_admin_key；
#     routes_events.py 全部端点走 require_agent_key（agent 拉取视角），
#     且任务约束「禁止改动 routes_events.py 现有端点行为」
#   - 查询参数 type 为可选类型过滤，subscriber_id 可选推进游标（pull 视角一并清空）
#
# operations 采用函数内导入（本文件既有惯例：demands/projects/dream 段），
# 使本次改动是文件尾部纯追加，不触碰顶部 import 块。


@router.post("/v1/admin/events/consume_all")
def consume_all_events(
    request: Request,
    type: str | None = Query(None, description="可选类型过滤（如 anomaly_warn）；None=全部类型"),
    subscriber_id: str | None = Query(None, description="可选订阅者：同步推进其持久游标到最新"),
    _: str = Depends(require_admin_key),
):
    """批量清空/全部消费信号（T-87）。

    - 全部未消费事件标记 consumed_at/consumed_by（幂等，二次调用 consumed=0）
    - type 过滤：只清空指定类型（如 anomaly_warn / care_daily）
    - subscriber_id：传则同步推进该订阅者持久游标（pull/SSE 视角一并清空）
    - consumed_by 从鉴权 key 反查（admin key → default；agent key 无权限）
    """
    from sgme.operations.events import events_consume_all as events_consume_all_operation

    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    agent_id = request.app.state.key_store.resolve_agent_id(request.headers.get("X-API-Key"))
    return run_operation(
        events_consume_all_operation, mem_conn,
        event_type=type, subscriber_id=subscriber_id, consumed_by=agent_id,
    )


# ---------- ST-34：自动更新意图文件端点 ----------
# WebUI「立即更新」确认 → POST 写 request.json（T-94 主机代理轮询执行）；
# WebUI 轮询更新状态 → GET 读 request.json。
# 业务实现在 sgme/operations/update_request.py；挂本文件（管理操作，admin 鉴权）。


class UpdateRequestModel(BaseModel):
    target_version: str


@router.post("/v1/admin/update/request")
def write_update_request(
    request: Request,
    body: UpdateRequestModel,
    _: str = Depends(require_admin_key),
):
    """写入自动更新意图文件（ST-34 T-93）：WebUI「立即更新」确认后调用。

    落 $SGME_HOME/update/request.json（原子写），供主机侧更新代理（T-94）轮询执行。
    幂等：重复调用覆盖为最新 target_version（status 重置 pending）。
    """
    from sgme.operations.update_request import write_update_request as write_update_request_op
    from sgme.config import USER_ROOT

    return run_operation(
        write_update_request_op, USER_ROOT, target_version=body.target_version,
    )


@router.get("/v1/admin/update/request")
def read_update_request(
    request: Request,
    _: str = Depends(require_admin_key),
):
    """读取自动更新意图文件（ST-34 T-93）：WebUI 轮询更新状态。

    无待执行请求 → 返回 {}（200）；有 → 返回 {target_version, requested_at, status}。
    """
    from sgme.operations.update_request import read_update_request as read_update_request_op
    from sgme.config import USER_ROOT

    result = read_update_request_op(USER_ROOT)
    if not result:
        return {"request": None}
    return {"request": result}
