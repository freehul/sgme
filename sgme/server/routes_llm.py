"""server/routes_llm.py：LLM 供应商与降级链端点（管理员 Key）。

- GET  /v1/admin/llm              → 降级链结构（chains）+ 链级规则（rules）+ 供应商连接信息（providers）
- GET  /v1/admin/llm/health       → 逐供应商健康探测（robust，同步探测每个非 rule 供应商）
- POST /v1/admin/llm/providers    → 新增/更新供应商连接信息（写回 providers.yaml）
- DELETE /v1/admin/llm/providers/{provider} → 删除供应商连接信息（被链引用时拒绝）

供应商/链概属 llm.yaml/providers.yaml 程序资源；本模块只读子集 + 供应商连接表
（providers.yaml）的增删管理入口。降级链（llm.yaml）本身仍由配置文件维护。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from sgme.operations.llm import (
    llm_health as llm_health_operation,
    llm_status as llm_status_operation,
    llm_provider_add as llm_provider_add_operation,
    llm_provider_delete as llm_provider_delete_operation,
    llm_embedding_set_active as llm_embedding_set_active_operation,
    llm_chain_update as llm_chain_update_operation,
)
from sgme.server.app import require_admin_key, run_operation

router = APIRouter(prefix="/v1/admin/llm", tags=["llm"])


@router.get("")
def get_llm(request: Request, _: str = Depends(require_admin_key)):
    """降级链结构 + 链级规则 + 供应商连接信息（只读）。"""
    cfg = request.app.state.cfg
    data = run_operation(llm_status_operation, cfg)
    return data


@router.get("/health")
def get_llm_health(request: Request, _: str = Depends(require_admin_key)):
    """逐供应商健康探测（同步，每个非 rule 供应商 GET 一次 /models）。"""
    cfg = request.app.state.cfg
    data = run_operation(llm_health_operation, cfg)
    return data


@router.post("/providers")
def post_provider(request: Request, body: dict[str, Any], _: str = Depends(require_admin_key)):
    """新增/更新供应商连接信息（写回 providers.yaml，密钥只存环境变量名）。"""
    cfg = request.app.state.cfg
    provider = str(body.get("provider") or "")
    payload = body.get("payload") or {}
    data = run_operation(llm_provider_add_operation, cfg, provider, payload)
    return data


@router.delete("/providers/{provider}")
def delete_provider(provider: str, request: Request, _: str = Depends(require_admin_key)):
    """删除供应商连接信息（被降级链引用时拒绝）。"""
    cfg = request.app.state.cfg
    data = run_operation(llm_provider_delete_operation, cfg, provider)
    return data


@router.put("/embedding/active")
def put_embedding_active(request: Request, body: dict[str, Any], _: str = Depends(require_admin_key)):
    """切换当前向量提供商（T-43，写回 search.vector 并落盘）。"""
    cfg = request.app.state.cfg
    provider = str(body.get("provider") or "")
    data = run_operation(llm_embedding_set_active_operation, cfg, provider)
    return data


@router.put("/chains")
def put_chains(request: Request, body: dict[str, Any], _: str = Depends(require_admin_key)):
    """整体更新降级链（T-44：增删节点 + 排序，写回 llm.yaml 并刷新运行时）。"""
    cfg = request.app.state.cfg
    chains = body.get("chains")
    data = run_operation(llm_chain_update_operation, cfg, chains)
    return data