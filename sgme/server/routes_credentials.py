"""server/routes_credentials.py：凭据模块端点（EP-11 T-270）——纯协议翻译。

- GET    /v1/credentials             列表（键名+元数据，不含值；agent 平级）
- GET    /v1/credentials/export      导出（?only=|?prefix= 互斥；agent；记审计+SecurityLog）
- POST   /v1/credentials/{key}/file  文件上传/替换（admin；body 原始字节；超限 413）
- GET    /v1/credentials/{key}/file  文件下载（agent；octet-stream + Content-Disposition）
- GET    /v1/credentials/{key}       取单值（agent；记审计；file 行 → 400 指路下载端点）
- POST   /v1/credentials             写入/更新（admin；已删名 → 409 引导 restore；不接受 file 类型）
- DELETE /v1/credentials/{key}       软删（admin；幂等）
- POST   /v1/credentials/{key}/restore  恢复（admin；幂等）

业务在 ``sgme.operations.credentials``（三段式），本文件只做协议翻译：
从 app.state 取依赖 → ``run_operation`` 调用 → ``http_payload`` 投影。

⚠️ 路由注册顺序：``/v1/credentials/export`` 静态路径与 ``/{key}/file``（D-37 v1.2）
**必须先于** ``/{key}`` 动态路径声明（T-174/T-253 先例——否则 export 被当作键名命中详情端点）。

SecurityLog admin_op 双轨（T-265 先例）：写 / 删 / 恢复 / export 四处接入；
export 虽为 agent 平级读取，但属批量敏感导出，安全面独立留痕。
"""
from __future__ import annotations

import sqlite3
import urllib.parse

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from sgme.operations.credentials import (
    delete_credential as delete_credential_operation,
    export_credentials as export_credentials_operation,
    get_credential as get_credential_operation,
    http_payload,
    list_credentials as list_credentials_operation,
    put_file as put_file_operation,
    read_file as read_file_operation,
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

# D-37 v1.2：单文件上限缺省值（与 sgme/config.py DEFAULT_CREDENTIALS_CONFIG 同值）
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024


# ---------- 请求模型 ----------

class CredentialWriteRequest(BaseModel):
    key_name: str
    value: str | None = None          # 省略 = 只更新元数据（仅限已存在键）
    description: str | None = None
    scope: str | None = None          # all / admin / agents:<id,...>；"" = 清空
    cred_type: str | None = None      # api_key / token / password / other；"" = 清空
    expires_at: str | None = None     # UTC ISO 8601；"" = 不过期


# ---------- 私有工具 ----------

def _creds_conn(request: Request) -> sqlite3.Connection:
    """取凭据库连接（模块停用/未接线时为 None——防御式守卫，正常路径不可达）。"""
    conn = getattr(request.app.state, "credentials_conn", None)
    if conn is None:
        raise api_error("ERR_INTERNAL", "凭据模块未启用或凭据库连接不可用")
    return conn


def _caller(request: Request) -> tuple[str, str, bool]:
    """调用方身份三元组 (actor, agent_id, is_admin)——D-37 v1.1 scope/过期判定用。

    agent_id 一律由 API key 反查（绝不读请求体自称，D-65 同纪律）；admin key 恒可读。
    """
    key = request.headers.get("X-API-Key")
    store: AgentKeyStore = request.app.state.key_store
    if store.is_admin(key):
        return "default(admin,http)", "default", True
    agent_id = store.resolve_agent_id(key) or "unknown"
    return f"{agent_id}(agent,http)", agent_id, False


def _actor(request: Request) -> str:
    """审计身份字符串（写 / 删 / 恢复端点用）。"""
    return _caller(request)[0]


def _max_file_bytes(request: Request) -> int:
    """文件单条上限（credentials.max_file_bytes；缺省 2 MiB）。

    配置非法（非整数）按缺省值处理——上限闸永不因配置笔误而放开。
    """
    cfg = getattr(request.app.state, "cfg", None) or {}
    section = cfg.get("credentials") or {}
    try:
        return int(section.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_FILE_BYTES


# ---------- 读侧（Agent Key 平级） ----------

@router.get("/v1/credentials")
def list_credentials(
    request: Request,
    _: str = Depends(require_agent_key),
):
    """列表（键名+元数据，不含值；默认过滤已删；list 不记账）。

    admin 看到全部 active（含 admin-only 与已过期，带 expired 标记）；
    agent 只看到 scope 可见且未过期的行。
    """
    _actor_s, agent_id, is_admin = _caller(request)
    return http_payload(
        run_operation(
            list_credentials_operation, _creds_conn(request),
            agent_id=agent_id, is_admin=is_admin,
        )
    )


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
    actor, agent_id, is_admin = _caller(request)
    target = f"only:{only}" if only else (f"prefix:{prefix}" if prefix else "all")
    try:
        data = run_operation(
            export_credentials_operation, conn, only=only, prefix=prefix, actor=actor,
            agent_id=agent_id, is_admin=is_admin,
        )
    except Exception:
        audit_admin_op(request, "credential_export", target=target, result="fail")
        raise
    audit_admin_op(request, "credential_export", target=target, result="success")
    return http_payload(data)


# ---------- 文件 / 二进制「重要数据」（D-37 v1.2） ----------
# ⚠️ 两条 /file 路由必须先于 /{key} 声明（见模块 docstring）


@router.post("/v1/credentials/{key}/file")
async def put_credential_file(
    key: str,
    request: Request,
    filename: str,
    description: str | None = None,
    scope: str | None = None,
    expires_at: str | None = None,
    _: str = Depends(require_admin_key),
):
    """文件上传 / 替换（admin）。body = 原始字节（绝不经 JSON base64）。

    Query：``filename`` 必填（服务端只取 basename、去控制字符、<=120 字符）；
    ``description`` / ``scope`` / ``expires_at`` 可选，合并语义同写值端点
    （缺省 = 不变、空串 = 清空）。
    单条上限取 ``credentials.max_file_bytes``（默认 2 MiB）→ 超限 413（ERR_TOO_LARGE）。
    已删键 → 409（引导 restore）；同键名为非 file 类型 → 409（不静默改型）。
    """
    conn = _creds_conn(request)
    actor = _actor(request)
    data = await request.body()
    try:
        result = run_operation(
            put_file_operation,
            conn,
            key_name=key,
            filename=filename,
            data=data,
            description=description,
            scope=scope,
            expires_at=expires_at,
            actor=actor,
            max_bytes=_max_file_bytes(request),
        )
    except Exception:
        audit_admin_op(request, "credential_file_write", target=key, result="fail")
        raise
    audit_admin_op(request, "credential_file_write", target=key, result="success")
    return http_payload(result)


@router.get("/v1/credentials/{key}/file")
def get_credential_file(
    key: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """文件下载（agent；受 scope / 过期约束）。返回 octet-stream 原始字节。

    响应头：``Content-Disposition: attachment; filename*=UTF-8''<pct-encoded>``、
    ``X-Cred-Sha256`` / ``X-Cred-Size``（客户端校验用，无需解析正文即知完整性）。
    """
    actor, agent_id, is_admin = _caller(request)
    data = run_operation(
        read_file_operation,
        _creds_conn(request),
        key,
        actor=actor,
        agent_id=agent_id,
        is_admin=is_admin,
    )
    return Response(
        content=data["data"],
        media_type="application/octet-stream",
        headers={
            # RFC 6266/5987：文件名可能含非 ASCII → 百分号编码进 filename*（头值保持 ASCII）
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + urllib.parse.quote(data["filename"]),
            "X-Cred-Sha256": data["sha256"],
            "X-Cred-Size": str(data["size"]),
        },
    )


@router.get("/v1/credentials/{key}")
def get_credential(
    key: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """取单值（记 read 审计）；不存在/已删 → 404；scope 拒绝/已过期 → 403。

    D-37 v1.2：file 类型凭据没有文本值 → 400 指路 GET /v1/credentials/{key}/file。
    """
    actor, agent_id, is_admin = _caller(request)
    return http_payload(
        run_operation(
            get_credential_operation, _creds_conn(request), key,
            actor=actor, agent_id=agent_id, is_admin=is_admin,
        )
    )


# ---------- 写侧（Admin Key） ----------

@router.post("/v1/credentials")
def write_credential(
    payload: CredentialWriteRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """写入/更新（admin）。已删键名 → 409 引导 restore（不自动复活）。

    D-37 v1.1：value 可省略（仅更新元数据）；scope/cred_type/expires_at
    缺省=不变、空串=清空。
    """
    conn = _creds_conn(request)
    actor = _actor(request)
    try:
        data = run_operation(
            write_credential_operation,
            conn,
            key_name=payload.key_name,
            value=payload.value,
            description=payload.description,
            scope=payload.scope,
            cred_type=payload.cred_type,
            expires_at=payload.expires_at,
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
