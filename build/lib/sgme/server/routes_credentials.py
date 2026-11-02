"""server/routes_credentials.py：凭据模块端点（EP-11 T-270）——纯协议翻译。

- GET    /v1/credentials             列表（键名+元数据，不含值；agent 平级）
- GET    /v1/credentials/export      导出（?only=|?prefix= 互斥；agent；记审计+SecurityLog）
- GET    /v1/credentials/{key}       取单值（agent；记审计）
- POST   /v1/credentials             写入/更新（admin；已删名 → 409 引导 restore）
- DELETE /v1/credentials/{key}       软删（admin；幂等）
- POST   /v1/credentials/{key}/restore  恢复（admin；幂等）

业务在 ``sgme.operations.credentials``（三段式），本文件只做协议翻译：
从 app.state 取依赖 → ``run_operation`` 调用 → ``http_payload`` 投影。

⚠️ 路由注册顺序：``/v1/credentials/export`` 静态路径**必须先于** ``/{key}``
动态路径声明（T-174/T-253 先例——否则 export 被当作键名命中详情端点）。

SecurityLog admin_op 双轨（T-265 先例）：写 / 删 / 恢复 / export 四处接入；
export 虽为 agent 平级读取，但属批量敏感导出，安全面独立留痕。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from sgme.operations.credentials import (
    delete_credential as delete_credential_operation,
    export_credentials as export_credentials_operation,
    get_credential as get_credential_operation,
    http_payload,
    list_credentials as list_credentials_operation,
    restore_credential as restore_credential_operation,
    write_credential as write_credential_operation,
)
from sgme.server.app import (
    AgentKeyStore,
    api_error,
    require_admin_key,
    require_agent_key,
    run_operation,
)

# T-265：admin_op 审计与 routes_admin 同源（事件形态一致）
from sgme.server.routes_admin import audit_admin_op

router = APIRouter()


# ---------- 请求模型 ----------

class CredentialWriteRequest(BaseModel):
    key_name: str
    value: str
    description: str | None = None


# ---------- 私有工具 ----------

def _creds_conn(request: Request) -> sqlite3.Connection:
    """取凭据库连接（模块停用/未接线时为 None——防御式守卫，正常路径不可达）。"""
    conn = getattr(request.app.state, "credentials_conn", None)
    if conn is None:
        raise api_error("ERR_INTERNAL", "凭据模块未启用或凭据库连接不可用")
    return conn


def _actor(request: Request) -> str:
    """审计身份：按调用方角色组装 ``default(admin,http)`` / ``<agent_id>(agent,http)``。"""
    key = request.headers.get("X-API-Key")
    store: AgentKeyStore = request.app.state.key_store
    if store.is_admin(key):
        return "default(admin,http)"
    agent_id = store.resolve_agent_id(key) or "default"
    return f"{agent_id}(agent,http)"


# ---------- 读侧（Agent Key 平级） ----------

@router.get("/v1/credentials")
def list_credentials(
    request: Request,
    _: str = Depends(require_agent_key),
):
    """列表（键名+元数据，不含值；默认过滤已删；list 不记账）。"""
    return http_payload(run_operation(list_credentials_operation, _creds_conn(request)))


# ⚠️ export 必须先于 /{key} 声明（见模块 docstring）
@router.get("/v1/credentials/export")
def export_credentials(
    request: Request,
    only: str | None = None,
    prefix: str | None = None,
    _: str = Depends(require_agent_key),
):
    """导出（JSON 原始值映射 {"credentials": {KEY: value}}，供客户端格式化 dotenv）。

    Query：``only``（逗号分隔键名清单，缺失→404）或 ``prefix``（前缀过滤）互斥。
    记领域审计 + SecurityLog admin_op 独立事件（批量敏感导出，安全面可见）。
    """
    conn = _creds_conn(request)
    actor = _actor(request)
    target = f"only:{only}" if only else (f"prefix:{prefix}" if prefix else "all")
    try:
        data = run_operation(
            export_credentials_operation, conn, only=only, prefix=prefix, actor=actor
        )
    except Exception:
        audit_admin_op(request, "credential_export", target=target, result="fail")
        raise
    audit_admin_op(request, "credential_export", target=target, result="success")
    return http_payload(data)


@router.get("/v1/credentials/{key}")
def get_credential(
    key: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """取单值（记 read 审计）；不存在/已删 → 404。"""
    return http_payload(
        run_operation(get_credential_operation, _creds_conn(request), key, actor=_actor(request))
    )


# ---------- 写侧（Admin Key） ----------

@router.post("/v1/credentials")
def write_credential(
    payload: CredentialWriteRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """写入/更新（admin）。已删键名 → 409 引导 restore（不自动复活）。"""
    conn = _creds_conn(request)
    actor = _actor(request)
    try:
        data = run_operation(
            write_credential_operation,
            conn,
            key_name=payload.key_name,
            value=payload.value,
            description=payload.description,
            actor=actor,
        )
    except Exception:
        audit_admin_op(request, "credential_write", target=payload.key_name, result="fail")
        raise
    audit_admin_op(request, "credential_write", target=payload.key_name, result="success")
    return http_payload(data)


@router.delete("/v1/credentials/{key}")
def delete_credential(
    key: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """软删（admin；幂等）。不存在 → 404。"""
    conn = _creds_conn(request)
    actor = _actor(request)
    try:
        data = run_operation(delete_credential_operation, conn, key_name=key, actor=actor)
    except Exception:
        audit_admin_op(request, "credential_delete", target=key, result="fail")
        raise
    audit_admin_op(request, "credential_delete", target=key, result="success")
    return http_payload(data)


@router.post("/v1/credentials/{key}/restore")
def restore_credential(
    key: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """恢复（admin；幂等，清空 deleted_at）。不存在 → 404。"""
    conn = _creds_conn(request)
    actor = _actor(request)
    try:
        data = run_operation(restore_credential_operation, conn, key_name=key, actor=actor)
    except Exception:
        audit_admin_op(request, "credential_restore", target=key, result="fail")
        raise
    audit_admin_op(request, "credential_restore", target=key, result="success")
    return http_payload(data)
