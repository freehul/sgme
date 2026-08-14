"""operations/project.py：项目注册表操作（0.8 ST-16，project_meta）。

承接的入口
----------
========================================  ===================================
入口                                       操作
========================================  ===================================
HTTP ``POST  /v1/admin/projects``          ``register_project``（upsert）
HTTP ``GET   /v1/admin/projects``          ``list_projects``（分页信封）
HTTP ``GET   /v1/admin/projects/{id}``     ``get_project``
HTTP ``PATCH /v1/admin/projects/{id}``     ``update_project``
========================================  ===================================

分层职责（照抄 health.py / memory.py 样板）：
1. 本模块**不认识协议**——不 import fastapi，不知道 HTTP 状态码；
   失败一律经 ``InvalidArgs`` / ``OperationResult.fail(ERR_*)`` 表达，
   由入口层 ``server/app.py::run_operation`` 翻译为 400 / 404。
2. 所有 SQL 在 ``sgme.data.project_dao``（铁律 B30）；本模块只做
   参数归一化 + 校验 + 信封组装。
3. **project_id 合法性校验落在本层**（不是 data 层、也不是路由层）：
   它是业务规则（「项目名必须是纯英文目录名」）而非存储约束，
   放这里可让 HTTP 与将来的 MCP 入口共享同一份校验与错误文案。

分页与错误码惯例严格对齐 ``SGME-接口契约-v0.1.md`` §5.3：
``page`` ≥ 1 默认 1；``limit`` 1-200 默认 50，越界 → 400 ``ERR_INVALID_ARGS``；
响应信封 ``{items, count, total, page, limit, generated_at}``。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs, OperationResult
from sgme.data import project_dao

# project_id = 项目名（纯英文，与 D:\Projects 目录名一致，数据模型 §二 project_meta）。
# 同时是主键与未来跨项目检索的关联键，因此限制为目录名安全字符集。
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PROJECT_ID_MAX_LEN = 64

# 分页（契约 §5.3.2：limit 上限硬限制 200，防查询放大）
DEFAULT_PAGE = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# 排序白名单（data 层同样白名单化，双保险）
SORT_FIELDS: tuple[str, ...] = project_dao.SORT_FIELDS
DEFAULT_SORT = "updated_at"
ORDERS: tuple[str, ...] = ("desc", "asc")
DEFAULT_ORDER = "desc"

# PATCH 允许的字段（与 data 层白名单同源，避免两处漂移）
UPDATABLE_FIELDS: tuple[str, ...] = project_dao.UPDATABLE_FIELDS
# 允许显式置空（清列）的字段：NOT NULL 列 name / path 不在其中
NULLABLE_FIELDS: tuple[str, ...] = ("git_repo", "last_active_at", "milestone")

# 单字段长度上限：轻量元数据表，防超长字符串灌爆（路径按 Windows 扩展长度上限留余量）
MAX_TEXT_LEN = 512


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（与 data 层同格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 参数归一化 / 校验 ----------

def _normalize_project_id(raw: Any) -> str:
    """校验并归一化 project_id。

    Args:
        raw: 原始入参（可能是 None / 非字符串）。

    Returns:
        去空白后的合法 project_id。

    Raises:
        InvalidArgs: 缺失、非字符串、超长或含非法字符（→ 400 ERR_INVALID_ARGS）。
    """
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise InvalidArgs("project_id 必填（项目名，纯英文）")
    value = raw.strip()
    if len(value) > PROJECT_ID_MAX_LEN:
        raise InvalidArgs(
            f"project_id 过长（最长 {PROJECT_ID_MAX_LEN} 字符）: {len(value)}"
        )
    if not PROJECT_ID_RE.match(value):
        raise InvalidArgs(
            f"project_id 非法: {value}（仅允许英文字母 / 数字 / 下划线 / 连字符）"
        )
    return value


def _normalize_text(
    raw: Any,
    field: str,
    required: bool = False,
    allow_none: bool = True,
) -> str | None:
    """校验并归一化普通文本字段（path / git_repo / milestone / name / 时间戳）。

    Args:
        raw: 原始入参。
        field: 字段名（错误文案用）。
        required: True 时 None / 空串 → InvalidArgs。
        allow_none: False 时显式 None → InvalidArgs（NOT NULL 列的 PATCH 清空拦截）。

    Returns:
        去空白后的字符串；未提供且非必填时返回 None。

    Raises:
        InvalidArgs: 类型错误 / 必填缺失 / 超长。
    """
    if raw is None:
        if required or not allow_none:
            raise InvalidArgs(f"{field} 必填，不可为空")
        return None
    if not isinstance(raw, str):
        raise InvalidArgs(f"{field} 必须是字符串: {type(raw).__name__}")
    value = raw.strip()
    if not value:
        if required or not allow_none:
            raise InvalidArgs(f"{field} 必填，不可为空")
        return None
    if len(value) > MAX_TEXT_LEN:
        raise InvalidArgs(f"{field} 过长（最长 {MAX_TEXT_LEN} 字符）: {len(value)}")
    return value


def _parse_int(
    raw: Any,
    field: str,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """解析并校验整数查询参数（惯例对齐 routes_admin._parse_active_within）。

    Args:
        raw: 原始查询串 / 整数 / None（None 与空串取默认值）。
        field: 字段名（错误文案用）。
        default: 缺省值。
        minimum: 下界（含）。
        maximum: 上界（含），None 表示不限。

    Returns:
        合法整数。

    Raises:
        InvalidArgs: 非整数或越界（→ 400 ERR_INVALID_ARGS）。
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidArgs(f"{field} 必须是整数: {raw}") from None
    if value < minimum:
        raise InvalidArgs(f"{field} 必须 ≥ {minimum}: {value}")
    if maximum is not None and value > maximum:
        raise InvalidArgs(f"{field} 上限 {maximum}: {value}")
    return value


def _normalize_sort(raw: Any) -> str:
    """校验排序字段（未知值 → 400，不静默回落，契约 §5.3.4）。"""
    if raw is None or raw == "":
        return DEFAULT_SORT
    value = str(raw).strip()
    if value not in SORT_FIELDS:
        raise InvalidArgs(
            f"sort 非法: {value}（可选 {' / '.join(SORT_FIELDS)}）"
        )
    return value


def _normalize_order(raw: Any) -> str:
    """校验排序方向（未知值 → 400）。"""
    if raw is None or raw == "":
        return DEFAULT_ORDER
    value = str(raw).strip().lower()
    if value not in ORDERS:
        raise InvalidArgs(f"order 非法: {value}（可选 {' / '.join(ORDERS)}）")
    return value


# ---------- 操作 1：登记项目（upsert） ----------

def register_project(
    mem_conn: sqlite3.Connection,
    project_id: Any = None,
    name: Any = None,
    path: Any = None,
    git_repo: Any = None,
    last_active_at: Any = None,
    milestone: Any = None,
) -> OperationResult:
    """登记项目到 project_meta（存在则更新，即 upsert）。

    为什么是 upsert 而不是 409 冲突：登记入口是 `scripts/project_init.py` 六步之④，
    立项脚本可能重跑（首次 admin key 未配置 / 网络失败后补登 / 目录迁移后重登），
    要求调用方先查再决定 POST/PATCH 会把幂等责任推给脚本，得不偿失。
    因此同 project_id 二次登记 = 更新，返回 ``created`` 标识区分新建/更新。

    Args:
        mem_conn: memory.db 连接。
        project_id: 项目名（纯英文），必填。
        name: 展示名，缺省同 project_id。
        path: 项目绝对路径。**新建时必填**（NOT NULL 列）；已存在时可省（保留原值）。
        git_repo: git 仓库地址（可空）。
        last_active_at: 最近活跃时刻（可空；图纸约定先留空，由探测链路回填）。
        milestone: 当前里程碑（可空）。

    Returns:
        OperationResult.succeed({"project": {...}, "created": bool, "generated_at": iso})

    Raises:
        InvalidArgs: 参数非法（入口层翻译为 400 ERR_INVALID_ARGS）。
    """
    pid = _normalize_project_id(project_id)
    existing = project_dao.get_project(mem_conn, pid)
    # 新建必须给 path（NOT NULL）；更新时省略 path 表示保留原值
    path_value = _normalize_text(path, "path", required=existing is None)
    name_value = _normalize_text(name, "name")
    git_repo_value = _normalize_text(git_repo, "git_repo")
    last_active_value = _normalize_text(last_active_at, "last_active_at")
    milestone_value = _normalize_text(milestone, "milestone")

    row, created = project_dao.upsert_project(
        mem_conn,
        project_id=pid,
        name=name_value,
        path=path_value,
        git_repo=git_repo_value,
        last_active_at=last_active_value,
        milestone=milestone_value,
    )
    return OperationResult.succeed(
        {"project": row, "created": created, "generated_at": _now_iso()}
    )


# ---------- 操作 2：分页列表 ----------

def list_projects(
    mem_conn: sqlite3.Connection,
    q: Any = None,
    milestone: Any = None,
    sort: Any = None,
    order: Any = None,
    page: Any = None,
    limit: Any = None,
) -> OperationResult:
    """分页列出项目注册表。

    Args:
        mem_conn: memory.db 连接。
        q: 名称子串过滤（同时匹配 project_id 与 name）。
        milestone: 里程碑精确过滤。
        sort: last_active_at / updated_at（默认）/ created_at。
        order: desc（默认）/ asc。
        page: 页码，≥ 1，默认 1。
        limit: 页大小，1-200，默认 50。

    Returns:
        OperationResult.succeed({items, count, total, page, limit, generated_at})

    Raises:
        InvalidArgs: 参数非法（→ 400 ERR_INVALID_ARGS）。
    """
    q_value = _normalize_text(q, "q")
    milestone_value = _normalize_text(milestone, "milestone")
    sort_value = _normalize_sort(sort)
    order_value = _normalize_order(order)
    page_value = _parse_int(page, "page", DEFAULT_PAGE, minimum=1)
    limit_value = _parse_int(limit, "limit", DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)

    items = project_dao.list_projects(
        mem_conn,
        q=q_value,
        milestone=milestone_value,
        sort=sort_value,
        order=order_value,
        page=page_value,
        limit=limit_value,
    )
    total = project_dao.count_projects(mem_conn, q=q_value, milestone=milestone_value)
    return OperationResult.succeed(
        {
            "items": items,
            "count": len(items),
            "total": total,
            "page": page_value,
            "limit": limit_value,
            "generated_at": _now_iso(),
        }
    )


# ---------- 操作 3：单条详情 ----------

def get_project(mem_conn: sqlite3.Connection, project_id: Any) -> OperationResult:
    """读取单条项目元数据。

    Args:
        mem_conn: memory.db 连接。
        project_id: 项目名（主键）。

    Returns:
        成功 OperationResult.succeed({"project": {...}, "generated_at": iso})；
        不存在 OperationResult.fail(ERR_NOT_FOUND)（入口层翻译为 404）。

    Raises:
        InvalidArgs: project_id 非法（→ 400）。
    """
    pid = _normalize_project_id(project_id)
    row = project_dao.get_project(mem_conn, pid)
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"项目不存在: {pid}")
    return OperationResult.succeed({"project": row, "generated_at": _now_iso()})


# ---------- 操作 4：部分更新（PATCH） ----------

def update_project(
    mem_conn: sqlite3.Connection,
    project_id: Any,
    fields: dict[str, Any] | None = None,
) -> OperationResult:
    """更新项目元数据的部分字段（path / git_repo / milestone / last_active_at / name）。

    PATCH 语义：``fields`` 里出现的键才更新；显式 None 表示清空该列
    （仅 NULLABLE_FIELDS 允许，name / path 是 NOT NULL 列，置空 → 400）。
    ``updated_at`` 恒刷新。

    Args:
        mem_conn: memory.db 连接。
        project_id: 项目名（主键）。
        fields: 待更新字段字典（来自请求体）。

    Returns:
        成功 succeed({"project": {...}, "updated_fields": [...], "generated_at": iso})；
        项目不存在 fail(ERR_NOT_FOUND)（→ 404）。

    Raises:
        InvalidArgs: 空 body / 未知字段 / 字段值非法 / 尝试改主键（→ 400）。
    """
    pid = _normalize_project_id(project_id)
    body = fields or {}
    if not isinstance(body, dict):
        raise InvalidArgs(f"请求体必须是对象: {type(body).__name__}")
    if not body:
        raise InvalidArgs(
            f"请求体为空，至少提供一个可更新字段（{' / '.join(UPDATABLE_FIELDS)}）"
        )

    unknown = [k for k in body if k not in UPDATABLE_FIELDS]
    if unknown:
        # project_id 单独给文案：改主键是常见误用，直说「不可改」比「未知字段」清晰
        if "project_id" in unknown:
            raise InvalidArgs("project_id 是主键，不可通过 PATCH 修改")
        raise InvalidArgs(
            f"未知字段: {', '.join(sorted(unknown))}"
            f"（可更新 {' / '.join(UPDATABLE_FIELDS)}）"
        )

    normalized: dict[str, Any] = {}
    for key in UPDATABLE_FIELDS:
        if key not in body:
            continue
        normalized[key] = _normalize_text(
            body[key], key, allow_none=key in NULLABLE_FIELDS
        )

    row = project_dao.update_project(mem_conn, pid, normalized)
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"项目不存在: {pid}")
    return OperationResult.succeed(
        {
            "project": row,
            "updated_fields": sorted(normalized),
            "generated_at": _now_iso(),
        }
    )
