"""server/routes_backup.py：备份恢复端点（Admin Key 鉴权）——纯协议翻译。

- POST /v1/admin/backup/create  创建快照（参数 level）
- GET  /v1/admin/backup/list     列出可用快照
- POST /v1/admin/backup/restore  从快照恢复（参数 snapshot_id，恢复前自动再备份）

全部 require_admin_key 鉴权（Agent Key 调用返回 403）。

v0.8 T-8：业务逻辑已全部下沉 ``sgme.operations.backup``（三段式），本模块只做
协议翻译——从 app.state 取依赖 → ``run_operation`` 调用 → ``http_payload``
投影。响应体与改造前逐字段一致（既有测试 test_backup.py / test_routes_backup.py
为契约基线，不得破坏）。

⚠️ restore 的 app.state 连接交换是入口层职责：operations 超集里的 ``_new_conns``
是私有传输字段（restore 已关闭旧连接并重开新连接），由本层取出更新 app.state，
再由 ``http_payload`` 剔除，不落入 HTTP 响应。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

# 0.8 方案 B：手动备份端点顺带幂等拉起每日自动备份定时器（同 Dream 接线模式）
from sgme.engine.backup_scheduler import ensure_scheduler as _ensure_backup_scheduler
from sgme.engine.backup_scheduler import stop_scheduler as _stop_backup_scheduler

# 既有测试 test_routes_backup.py 直接引用本模块的 _resolve_backup_dir
# （验证系统临时区告警行为），v0.8 迁移到 operations 后此处保留 re-export 兼容，
# 函数本体与告警行为在 operations/backup.py（业务逻辑不留在入口层）。
from sgme.operations.backup import (
    _parse_level,  # noqa: F401  （re-export 兼容，路由本体不使用）
    _resolve_backup_dir,  # noqa: F401  （re-export 兼容，路由本体不使用）
    backup_create as backup_create_operation,
    backup_list as backup_list_operation,
    backup_restore as backup_restore_operation,
    http_payload,
)
from sgme.server.app import require_admin_key, run_operation

router = APIRouter()


# ---------- 请求模型 ----------

class BackupCreateRequest(BaseModel):
    level: str = "incremental"


class BackupRestoreRequest(BaseModel):
    snapshot_id: str


# ---------- POST /v1/admin/backup（契约 §5）与 /v1/admin/backup/create ----------

@router.post("/v1/admin/backup")
@router.post("/v1/admin/backup/create")
def backup_create(
    payload: BackupCreateRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """创建快照（契约 §5 路径 /v1/admin/backup 与实现路径 /create 等价）。

    纯协议翻译：取依赖 → run_operation → http_payload 投影。
    """
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn
    wiki_conn: sqlite3.Connection = request.app.state.wiki_conn

    # 0.8 方案 B：幂等拉起每日自动备份定时器（生产 Gateway 首次备份后常驻）
    # v1.0 连接隔离修复（2026-08-14）：传 data_dir，线程自建独立连接，不再共享宿主连接
    _ensure_backup_scheduler(cfg, data_dir=request.app.state.data_dir)

    data = run_operation(
        backup_create_operation,
        cfg,
        mem_conn,
        session_conn,
        wiki_conn,
        level=payload.level,
    )
    return http_payload(data)


# ---------- GET /v1/admin/backup/list ----------

@router.get("/v1/admin/backup/list")
def backup_list(
    request: Request,
    _: str = Depends(require_admin_key),
):
    """列出可用快照。纯协议翻译（业务逻辑见 operations.backup.backup_list）。"""
    cfg = request.app.state.cfg

    data = run_operation(backup_list_operation, cfg)
    return http_payload(data)


# ---------- POST /v1/admin/backup/restore ----------

@router.post("/v1/admin/backup/restore")
def backup_restore(
    payload: BackupRestoreRequest,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """从快照恢复（恢复前自动再备份当前状态）。

    纯协议翻译 + 入口层职责（app.state 连接交换）：
    operations 返回的超集含 ``_new_conns``（restore 重开的三库连接），
    由本层取出更新 app.state，http_payload 负责将其剔除出响应体。
    """
    cfg = request.app.state.cfg
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn
    wiki_conn: sqlite3.Connection = request.app.state.wiki_conn

    # restore 替换数据库文件，先停 backup_scheduler（其自建连接持有 WAL 句柄，
    # 不停止会 unlink 失败 PermissionError；restore 后下次备份端点触发时自动重启）
    _stop_backup_scheduler(timeout=2.0)

    data = run_operation(
        backup_restore_operation,
        cfg,
        mem_conn,
        session_conn,
        wiki_conn,
        snapshot_id=payload.snapshot_id,
    )

    # 更新 app.state 中的 conn 引用（旧 conn 已被 restore 关闭）
    new_conns = data.get("_new_conns")
    if new_conns:
        request.app.state.mem_conn, request.app.state.session_conn, request.app.state.wiki_conn = new_conns

    return http_payload(data)
