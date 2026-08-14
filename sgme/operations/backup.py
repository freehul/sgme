"""operations/backup.py：备份恢复操作（0.8 T-8，契约 §5 备份端点）。

承接的入口
----------
========================================  ==========================================
入口                                       操作
========================================  ==========================================
HTTP ``POST /v1/admin/backup``            ``backup_create``（契约 §5 路径）
HTTP ``POST /v1/admin/backup/create``     ``backup_create``（实现路径，与 §5 等价）
HTTP ``GET  /v1/admin/backup/list``       ``backup_list``
HTTP ``POST /v1/admin/backup/restore``    ``backup_restore``
========================================  ==========================================

三段式结构（照抄 health.py 样板，复制时保持）：
1. 常量/私有工具函数（_resolve_backup_dir / _parse_level，本模块内聚，不外泄）
2. ``xxx(...) -> OperationResult`` 操作函数：显式接参（cfg + 三库连接），
   返回**协议无关的信息超集**（HTTP 响应所需全部字段 + restore 的 _new_conns 传输字段）
3. ``http_payload(data)`` 投影函数：把超集裁剪成 HTTP 历史契约形态

⚠️ B30 裸连接说明（既有注释，必须保留）
----------------------------------------
backup 是「裸连接绕过 data 层的**唯一允许场景**」：SQLite backup API 需要原生
``sqlite3.Connection``，且打开源库绝不能触发 ``data/storage`` 的迁移链
（备份期间改库结构 = 灾难）。全部裸连接逻辑封装在 ``sgme.backup.manager``
（``_backup_db`` / ``create_snapshot`` / ``restore`` 内各有 B30 注释），
本模块只做参数装配与结果归一，**不新增任何裸连接代码**。

⚠️ 本模块无 MCP 形态（无 mcp_payload 投影）
--------------------------------------------
backup 端点全部 ``require_admin_key`` 鉴权（Agent Key 调用返回 403），
MCP 工具面向 Agent Key，故 backup 不存在 MCP 出口；超集 == HTTP 形态，
``http_payload`` 仅负责剔除 restore 的 ``_new_conns`` 私有传输字段。

依赖方向：``sgme.backup.manager``（业务核心）+ ``sgme.config``（路径解析），
符合 模块边界铁律（operations → engine/data/...；backup 裸连接例外见上）。

副作用（抽取后必须保留）：``_resolve_backup_dir`` 落盘前若最终目录位于系统临时区
打 WARNING（防 HEAD 带临时路径时静默备份进回收区）——该行为有既有测试覆盖。
"""
from __future__ import annotations

import logging
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from sgme import config as sgme_config
from sgme.backup import manager
from sgme.operations.errors import ERR_NOT_FOUND, OperationResult

logger = logging.getLogger("sgme.operations.backup")


# ---------- 私有工具 ----------

def _resolve_backup_dir(cfg: dict[str, Any]) -> Path:
    """从 cfg 解析 backup 目录（相对路径基于用户根，T-23 跟随 SGME_HOME）。

    绝对路径合法（用户可把备份放到其他盘），但落盘前若最终目录位于系统临时区，
    打 WARNING 提示可能被系统清理——防 HEAD 带临时路径时静默备份进回收区
    （memory.db / wiki.db 备份进 Windows 临时区，用户以为备份了实际随时被清掉）。
    """
    backup_cfg = cfg.get("backup", {})
    dir_str = backup_cfg.get("dir", "data/backups")
    p = Path(dir_str)
    if not p.is_absolute():
        p = sgme_config.USER_ROOT / p
    # 落盘前校验：最终目录若位于系统临时区，告警（不阻断，仅提示）
    _tmp_root = Path(tempfile.gettempdir())
    try:
        p.resolve().relative_to(_tmp_root.resolve())
        logger.warning(
            "备份目录位于系统临时区 %s，可能被系统清理，建议改用持久目录（当前: %s）",
            _tmp_root, p,
        )
    except ValueError:
        pass
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_level(name: str) -> str:
    """从 snapshot_id 名称解析 level。"""
    if name.startswith("pre_restore_"):
        return "pre_restore"
    if name.startswith("incremental_"):
        return "incremental"
    if name.startswith("full_"):
        return "full"
    if name.startswith("monthly_"):
        return "monthly"
    return "unknown"


# ---------- 操作函数 ----------

def backup_create(
    cfg: dict[str, Any],
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    wiki_conn: sqlite3.Connection,
    level: str = "incremental",
) -> OperationResult:
    """创建快照（契约 §5 路径 /v1/admin/backup 与实现路径 /create 等价）。

    签名刻意**只收业务依赖**：operations 层不认识 ``request.app.state``
    （那是入口层的协议细节），由入口层取出后显式传入。

    Args:
        cfg: 运行时配置（backup.dir / backup.remote_dir / paths.data_dir）。
        mem_conn: memory.db 连接（SQLite backup API 源）。
        session_conn: session.db 连接。
        wiki_conn: wiki.db 连接。
        level: 快照级别（full / monthly / incremental），默认 incremental。

    Returns:
        OperationResult(ok=True)，data 为协议无关信息超集（即 HTTP 历史形态）：
        - snapshot_id / level / path / created_at / files：manager.create_snapshot 原样
        - push_remote: 异地副本推送结果（失败不阻塞本地备份；
          remote_dir 未配置时为 {"ok": True, "skipped": True}）

        本操作不返回失败态：create_snapshot 异常按改造前行为**继续向上抛**，
        由入口层全局异常处理器兜底（刻意不加 catch-all，避免改变错误响应形态）。
    """
    backup_dir = _resolve_backup_dir(cfg)

    result = manager.create_snapshot(
        data_dir=cfg["paths"]["data_dir"],
        dest_dir=backup_dir,
        level=level,
        conn_pair=(mem_conn, session_conn, wiki_conn),
    )

    # 异地副本推送（失败不阻塞本地备份）
    remote_dir = cfg.get("backup", {}).get("remote_dir") or None  # 空字符串→None，走跳过分支
    result["push_remote"] = manager.push_remote(result["path"], remote_dir)

    return OperationResult.succeed(result)


def backup_list(cfg: dict[str, Any]) -> OperationResult:
    """列出可用快照。

    Args:
        cfg: 运行时配置（backup.dir）。

    Returns:
        OperationResult(ok=True)，data 为协议无关信息超集（即 HTTP 历史形态）：
        - snapshots: [{snapshot_id, level, path}, ...]，按目录名降序（名称含时间戳，
          字典序 = 时间序，最新的在前），level 由名称前缀解析
        - total: 快照数量
    """
    backup_dir = _resolve_backup_dir(cfg)

    snapshots = []
    if backup_dir.exists():
        for d in sorted(backup_dir.iterdir(), key=lambda p: p.name, reverse=True):
            if d.is_dir():
                snapshots.append({
                    "snapshot_id": d.name,
                    "level": _parse_level(d.name),
                    "path": str(d),
                })

    return OperationResult.succeed({"snapshots": snapshots, "total": len(snapshots)})


def backup_restore(
    cfg: dict[str, Any],
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    wiki_conn: sqlite3.Connection,
    snapshot_id: str,
) -> OperationResult:
    """从快照恢复（恢复前自动再备份当前状态）。

    Args:
        cfg: 运行时配置（backup.dir / paths.data_dir / RAW_DIR 经 sgme_config）。
        mem_conn / session_conn / wiki_conn: 当前三库连接（restore 会关闭它们）。
        snapshot_id: 快照目录名（backup.dir 下的子目录）。

    Returns:
        OperationResult：
        - 成功：data 含 restored{files, snapshot_id} / pre_restore_snapshot /
          **_new_conns**（restore 重开的三库连接三元组——入口层用它更新
          app.state 引用，属于协议无关超集里的**私有传输字段**，
          http_payload 投影时会剔除，绝不落入 HTTP 响应）。
        - 快照不存在：OperationResult(ok=False, error_code=ERR_NOT_FOUND,
          message="快照不存在: {snapshot_id}")，入口层 run_operation 翻译为 404。
    """
    backup_dir = _resolve_backup_dir(cfg)

    snapshot_path = backup_dir / snapshot_id
    if not snapshot_path.exists() or not snapshot_path.is_dir():
        return OperationResult.fail(ERR_NOT_FOUND, f"快照不存在: {snapshot_id}")

    result = manager.restore(
        snapshot_path=snapshot_path,
        data_dir=cfg["paths"]["data_dir"],
        raw_dir=str(sgme_config.RAW_DIR),
        conn_pair=(mem_conn, session_conn, wiki_conn),
    )

    return OperationResult.succeed(result)


# ---------- 投影 ----------

def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP 历史契约形态（改造前 routes_backup 逐字段等价）。

    backup 无 MCP 出口，超集与 HTTP 形态本为一体；本函数唯一职责是剔除
    ``_new_conns``（restore 重开连接的传输字段，只用于入口层 app.state 交换，
    不属于响应体）。字段顺序由 dict 推导保持，与改造前一致。
    """
    return {k: v for k, v in data.items() if k != "_new_conns"}
