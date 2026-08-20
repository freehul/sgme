"""operations/update_request.py：自动更新意图文件读写（ST-34 T-93）。

职责：WebUI「立即更新」确认后，把更新请求落到 SGME_HOME/update/request.json，
供主机侧更新代理（T-94 scripts/sgme-host-updater）轮询执行。

意图文件契约（与 T-94 主机代理对齐）：
- 路径：$SGME_HOME/update/request.json（未设 SGME_HOME → 项目根/update/request.json）
- 内容：{target_version, requested_at, status}
  - status: "pending"（待执行）| "done"（成功）| "failed"（失败）
  - 主机代理执行后更新 status + 写 result；WebUI 轮询读取展示
- 原子写：先写临时文件再 os.replace（防半写文件被主机代理读到）
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sgme.operations.errors import OperationResult

# 意图文件相对 SGME_HOME / 项目根的路径
UPDATE_DIR_NAME = "update"
REQUEST_FILE_NAME = "request.json"


def _update_dir(user_root: Path) -> Path:
    return user_root / UPDATE_DIR_NAME


def _request_path(user_root: Path) -> Path:
    return _update_dir(user_root) / REQUEST_FILE_NAME


def write_update_request(
    user_root: Path,
    target_version: str,
    status: str = "pending",
) -> OperationResult:
    """写入更新意图文件（WebUI 确认「立即更新」后调用）。

    Args:
        user_root: 用户数据根（config.USER_ROOT；SGME_HOME 或项目根）。
        target_version: 目标版本号（如 "v1.0.0b5"）。
        status: 初始状态（默认 pending）。

    Returns:
        OperationResult(ok=True)，data 为 {path, target_version, status}。
        写失败（权限/磁盘）→ OperationResult(ok=False)，不抛异常。
    """
    path = _request_path(user_root)
    payload = {
        "target_version": target_version,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # 原子替换，防半写
        return OperationResult.succeed(
            {
                "path": str(path),
                "target_version": target_version,
                "status": status,
            }
        )
    except Exception as exc:  # noqa: BLE001 —— 写失败返回失败态不抛
        return OperationResult.fail(
            error_code="ERR_UPDATE_REQUEST_WRITE",
            message=f"写入更新意图文件失败: {exc}",
        )


def read_update_request(user_root: Path) -> dict[str, Any]:
    """读取更新意图文件（WebUI 轮询 / 主机代理读取）。

    文件不存在 → 返回 {}（无待执行更新请求）。
    文件损坏（JSON 解析失败）→ 返回 {}（静默降级，不抛异常）。
    """
    path = _request_path(user_root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
        return {}
    except Exception:  # noqa: BLE001
        return {}


def clear_update_request(user_root: Path) -> OperationResult:
    """清除更新意图文件（更新完成/失败后，主机代理调用）。

    Returns:
        OperationResult(ok=True)。文件不存在也返回 ok（幂等）。
    """
    path = _request_path(user_root)
    try:
        if path.exists():
            path.unlink()
        return OperationResult.succeed({"cleared": True})
    except Exception as exc:  # noqa: BLE001
        return OperationResult.fail(
            error_code="ERR_UPDATE_REQUEST_CLEAR",
            message=f"清除更新意图文件失败: {exc}",
        )
