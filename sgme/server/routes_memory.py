"""server/routes_memory.py：MemoryHub 端点。

- POST /v1/append    L0 捕获（Agent Key）
- POST /v1/inject    记忆注入（Agent Key）
- POST /v1/search    检索（Agent Key）
- GET  /v1/memory/{memory_id}  单条记忆 + 溯源（Agent Key）
- GET  /v1/sessions/{file_id}  L0 原文读取（Agent Key，0.8 ST-9）
- GET  /v1/health    健康检查（Bearer 即可，不强制 X-API-Key）
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("sgme.server.routes_memory")

from sgme import config as sgme_config
# 规范：operations 一律走完整子模块路径导入（包级不扁平导出操作函数）
# ——详见 operations/__init__.py「导入规范」。
from sgme.operations.health import health as health_operation
from sgme.operations.health import http_payload as health_http_payload
from sgme.operations.memory import get_http_payload as memory_get_http_payload
from sgme.operations.memory import get_memory as get_memory_operation
from sgme.operations.memory import reject_memory as reject_memory_operation
from sgme.operations.memory import unreject_memory as unreject_memory_operation
from sgme.operations.append import append_l0 as append_l0_operation
from sgme.operations.inject import inject as inject_operation
from sgme.operations.search import http_payload as search_http_payload
from sgme.operations.search import search as search_operation
from sgme.operations.session import get_raw_file_content as get_raw_file_content_operation
from sgme.server.app import AgentKeyStore, api_error, require_agent_key, require_bearer, run_operation

router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 请求/响应模型 ----------

class AppendRequest(BaseModel):
    session_key: str
    agent_id: str | None = None
    agent_model: str | None = None  # T-43：声明的提炼模型（provider/model），未传则按 agent_id 反查
    started_at: str
    ended_at: str | None = None
    source_type: str = "session"
    content: str
    metadata: dict[str, Any] | None = None


class InjectRequest(BaseModel):
    mode: str | None = None
    max_tokens: int | None = None
    custom_filter: dict | None = None


class SearchRequest(BaseModel):
    query: str
    scopes: list[str] = Field(default_factory=lambda: ["memory", "skills"])
    dimensions: list[str] | None = None
    match: str = "any"
    limit: int = 10
    include_sources: bool = True


# ---------- POST /v1/append ----------

@router.post("/v1/append")
def append_session(
    payload: AppendRequest,
    request: Request,
    auth_key: str = Depends(require_agent_key),
):
    """L0 捕获：写 raw 文件 + raw_files 索引。

    v0.7：业务逻辑已下沉 ``sgme.operations.append``，本函数只做协议翻译。
    幂等：同 session_key + 同 started_at → 不重复生成文件段，返回既有 file_id。
    同 session_key + 不同 started_at → 追加到既有文件（status 重置为 new）。

    B35 溯源兜底（2026-08-11）：body.agent_id 缺省时按鉴权 key 反查——
    注册 agt_* key → 绑定 agent_id；env 主 key/admin key → "default"。
    关掉「HTTP 调用不报 agent_id 就落 NULL」的口子。
    """
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn
    store: AgentKeyStore = request.app.state.key_store

    agent_id = payload.agent_id or store.resolve_agent_id(auth_key)
    # T-43：agent_model 显式传 → 用之；未传 → 按 agent_id 反查注册声明
    agent_model = payload.agent_model or store.resolve_agent_model(agent_id)

    return run_operation(
        append_l0_operation,
        session_key=payload.session_key,
        started_at=payload.started_at,
        content=payload.content,
        source_type=payload.source_type,
        ended_at=payload.ended_at,
        agent_id=agent_id,
        metadata=payload.metadata,
        agent_model=agent_model,
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
    )


# ---------- POST /v1/inject ----------

@router.post("/v1/inject")
def inject_memories(
    payload: InjectRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """记忆注入：mode 或 custom_filter → 模板查询 → blocks。

    v0.7：业务逻辑已下沉 ``sgme.operations.inject``，本函数只做协议翻译。
    """
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn

    return run_operation(
        inject_operation,
        mem_conn,
        cfg,
        mode=payload.mode,
        custom_filter=payload.custom_filter,
    )


# ---------- POST /v1/search ----------

@router.post("/v1/search")
def search_memories(
    payload: SearchRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """检索：四层（memory 记忆 + wiki 场景 + wiki_pages 知识库），标签过滤 + FTS5 BM25 + 向量 + RRF。

    v0.7：业务逻辑已下沉 ``sgme.operations.search``，本函数只做协议翻译；
    响应经 ``search_http_payload`` 投影为 HTTP 历史契约形态。
    T-34：wiki_pages 层经 wiki_conn 注入（wiki 扩展未挂载时为空结果，不影响整体）。
    """
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)

    data = run_operation(
        search_operation,
        mem_conn,
        session_conn,
        cfg,
        query=payload.query,
        scopes=payload.scopes,
        dimensions=payload.dimensions,
        match=payload.match,
        limit=payload.limit,
        include_sources=payload.include_sources,
        wiki_conn=wiki_conn,
        # T-112：技能层走 skills.db（FTS5 + 持久化向量）；禁用时为 None，操作层回退内存索引
        skills_conn=getattr(request.app.state, "skills_conn", None),
    )
    return search_http_payload(data)


# ---------- GET /v1/memory/{memory_id} ----------

@router.get("/v1/memory/{memory_id}")
def get_memory(
    memory_id: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """单条记忆 + 溯源 + 归档链。

    v0.7：业务逻辑已下沉 ``sgme.operations.memory``，本函数只做协议翻译。
    响应体与 v0.6 逐字段等价（HTTP 是三键包裹体，与 MCP 的裸记忆对象存在
    历史差异，故需 ``get_http_payload`` 投影）。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    data = run_operation(get_memory_operation, mem_conn, memory_id)
    return memory_get_http_payload(data)


# ---------- POST /v1/memory/{memory_id}/reject ----------

@router.post("/v1/memory/{memory_id}/reject")
def reject_memory(
    memory_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_agent_key),
):
    """用户纠错「不采用」：标记记忆为 rejected（不删除、可恢复）。

    2026-08-06 新增（用户明确：删除会造成更多问题，打标记以后不加载显示）。
    - 数据完整保留，仅 status='rejected'；查询/搜索/候选池一律过滤
    - body: {"reason": "用户说明的纠错原因"}（可选，默认"用户纠错"）
    - 幂等：重复 reject 更新 reason

    v0.7：业务逻辑已下沉 ``sgme.operations.memory``；本端点无 MCP 对端，
    操作 data 即响应体，无需投影。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        reject_memory_operation, mem_conn, memory_id,
        reason=(body or {}).get("reason"),
    )


# ---------- POST /v1/memory/{memory_id}/unreject ----------

@router.post("/v1/memory/{memory_id}/unreject")
def unreject_memory(
    memory_id: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """撤销「不采用」：恢复为 active（rejected 误操作时用）。

    v0.7：业务逻辑已下沉 ``sgme.operations.memory``；本端点无 MCP 对端，
    操作 data 即响应体，无需投影。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(unreject_memory_operation, mem_conn, memory_id)


# ---------- GET /v1/sessions/{file_id} ----------

@router.get("/v1/sessions/{file_id}")
def get_session_raw(
    file_id: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """L0 原文读取：按 file_id 取回会话原文全文（契约 §4.7，0.8 ST-9）。

    纯只读，无副作用。响应 ``{file_id, session_key, agent_id, content}``——
    元数据取自 session.db 的 raw_files，正文从磁盘 ``raw/sessions/{file_id}.md`` 读。

    **不做鉴权归属校验**：单用户语义下 agent key 可读任何 file_id
    （与 SCSM 契约一致，多租户留待 v2）——这是契约 §4.7 的明确要求，不是遗漏。

    业务逻辑在 ``sgme.operations.session``（与 admin 版 §5.6.2 共用同一实现），
    本函数只做协议翻译：取依赖 → 调 operation → run_operation 翻译错误码。
    file_id 不存在 → ERR_NOT_FOUND → HTTP 404。
    """
    session_conn: sqlite3.Connection = request.app.state.session_conn

    return run_operation(
        get_raw_file_content_operation,
        session_conn,
        file_id,
        sgme_config.RAW_DIR,
    )


# ---------- GET /v1/health ----------

@router.get("/v1/health")
def health_check(request: Request):
    """健康检查：Bearer 即可（不强制 X-API-Key）。

    返回提炼水位 watermark_age_sec + queue_depth + 心跳字段（stalled / heartbeat_ok）。

    v0.7：业务逻辑已下沉至 ``sgme.operations.health``，本函数只做协议翻译——
    从 app.state 取依赖 → 调 operation → 投影为 HTTP 历史契约形态。
    响应体与 v0.6 逐字段等价（HTTP 的 refinement 是重组超集，与 MCP 原始透传版
    存在历史差异，故需 http_payload 投影；统一属 v0.8 议题，详见 operations/health.py）。
    """
    require_bearer(request)
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn

    data = run_operation(health_operation, mem_conn, session_conn, cfg)
    return health_http_payload(data)
