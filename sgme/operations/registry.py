"""operations/registry.py：维度注册表管理操作（0.8 T-8 registry 链路）。

承接的入口
----------
=============================================  ==================================
入口                                            操作
=============================================  ==================================
HTTP ``GET /v1/admin/registry``                 ``registry_list`` + ``http_payload``
HTTP ``GET /v1/admin/registry/{dim_id}``        ``registry_get`` + ``http_payload``
HTTP ``POST /v1/admin/registry/dimensions``     ``registry_create_dim`` + ``http_payload``
HTTP ``PUT /v1/admin/registry/dimensions/{id}`` ``registry_update_dim`` + ``http_payload``
HTTP ``POST /v1/admin/registry/aliases``        ``registry_create_alias`` + ``http_payload``
HTTP ``DELETE /v1/admin/registry/aliases/{a}``  ``registry_delete_alias`` + ``http_payload``
=============================================  ==================================

注册表是**仅管理员 Key 可调**的 HTTP 专属链路（契约 §5），MCP 无对应工具，
故本模块只提供 ``http_payload`` 投影，不写 ``mcp_payload``（无消费方，写了是死代码）。

三段式结构（照抄 health.py 样板）：
1. 常量/私有工具（本模块内聚，不外泄）
2. ``xxx(...) -> OperationResult`` 操作函数：显式接参，返回**协议无关的信息超集**
3. ``http_payload(data)`` 投影函数：把超集裁剪成 HTTP 的历史契约形态

设计（§8.1 维度可扩展决策）：
- 用户 UI / SCSM 经此接口动态新增维度（注册表加行 + 别名打标即可，无需 schema 迁移）
- 维度 id 必须是 snake_case（防注入/防不规范键）
- 停用（active=false）而非删除——历史记忆的维度标签保留可溯源

副作用（抽取后必须保留，不得"优化"掉）：
- 写路径（create/update/alias 增删）落库提交后统一调 ``refresh_dimensions``
  刷新 ``cfg['dimensions']``（#33：否则运行时新增维度不注入 L1 prompt）——
  这是既有可观测行为，健康检查等依赖同一 cfg 的路径以此为据。

依赖：只调 ``sgme.data.memory_dao``（data 是唯一数据库操作层）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from sgme.data import memory_dao
from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs, OperationResult

logger = logging.getLogger("sgme.operations.registry")

# ---------- 1. 常量 / 私有工具（本模块内聚，不外泄） ----------

# 维度 id 白名单：snake_case，1-32 字符（原 routes_registry 逐字抄录，勿改）
_DIM_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_VALID_CATEGORIES = {"静态", "偏好", "动态"}
_VALID_VELOCITY = {"static", "dynamic"}

# 冻结的 HTTP 历史契约字段集合（改造前逐字段抄录，任何变动即破坏性变更）
# 维度行 = dimension_registry 全列 + 追加 aliases
DIMENSION_FIELD_KEYS = [
    "id", "display_name", "category", "time_velocity",
    "ttl_days", "description", "active", "created_at",
    "boundaries",  # T-11：维度边界（vs 对照），随 DB 行暴露（表列序末尾）
    "aliases",
]
LIST_TOP_KEYS = ["total", "dimensions"]
GET_TOP_KEYS = ["dimension"]
CREATE_TOP_KEYS = ["status", "dimension"]
UPDATE_TOP_KEYS = ["status", "dimension_id", "updates"]
ALIAS_TOP_KEYS = ["status", "alias", "dimension_id"]
DELETE_TOP_KEYS = ["status", "alias", "deleted"]


def _validate_dimension_id(dim_id: str) -> None:
    """校验维度 id 为 snake_case，非法抛 InvalidArgs（→ HTTP 400）。"""
    if not _DIM_ID_RE.match(dim_id):
        raise InvalidArgs(f"非法维度 id: {dim_id!r}（需 snake_case，1-32 字符）")


def _normalize_dimension(dim: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化维度入参（id/category/time_velocity 白名单校验）。

    Args:
        dim: 入口层解析后的原始入参（键：id/display_name/category/
            time_velocity/ttl_days/description/boundaries——boundaries 可选）。

    Returns:
        归一化后的维度字典（键序即 HTTP 响应中 dimension 对象的键序）。

    Raises:
        InvalidArgs: id 非 snake_case / category / time_velocity 非法。
    """
    _validate_dimension_id(dim["id"])
    if dim["category"] not in _VALID_CATEGORIES:
        raise InvalidArgs(f"category 须为 {sorted(_VALID_CATEGORIES)}")
    if dim["time_velocity"] not in _VALID_VELOCITY:
        raise InvalidArgs(f"time_velocity 须为 {sorted(_VALID_VELOCITY)}")
    return {
        "id": dim["id"],
        "display_name": dim["display_name"],
        "category": dim["category"],
        "time_velocity": dim["time_velocity"],
        "ttl_days": dim["ttl_days"],
        "description": dim["description"],
        # T-11：boundaries 可选入参，保留并落库（缺失时 None，兼容旧入参）
        "boundaries": dim.get("boundaries"),
    }


def refresh_dimensions(cfg: dict[str, Any], mem_conn: sqlite3.Connection) -> None:
    """从 dimension_registry 表重读 active 维度刷新 cfg['dimensions']（#33 修复启动快照缺口）。

    现状缺口：cfg['dimensions'] 是启动时一次性快照，/v1/admin/registry 写库后不刷新，
    运行时新增维度不注入 L1 prompt。写库成功后必须调用本函数——
    L1 render 每次从 cfg['dimensions'] 生成维度清单，"注册表变更自动刷新提示词"由此闭环。

    兼容性说明：本函数 v0.6 起定义在 ``routes_registry`` 并被他处引用
    （tests/test_prompts_qa_acceptance.py 直接 import），抽取后由 routes_registry
    原样 re-export，调用方零改动。
    """
    cfg["dimensions"] = memory_dao.list_dimensions(mem_conn, active_only=True)


def _dimension_with_aliases(mem_conn: sqlite3.Connection, dim: dict[str, Any]) -> dict[str, Any]:
    """维度行 + 别名列表（HTTP 历史契约中"每个维度带 aliases"的形态）。"""
    aliases = [r["alias"] for r in memory_dao.list_aliases_by_dimension(mem_conn, dim["id"])]
    return {**dim, "aliases": aliases}


# ---------- 2. 操作函数（返回 OperationResult，协议无关信息超集） ----------

def registry_list(mem_conn: sqlite3.Connection, *, active_only: bool = True) -> OperationResult:
    """列出全部维度（含别名）。active_only=false 时含停用维度。

    data 形态（即 HTTP 历史契约）：
    - total: 维度总数
    - dimensions: [{dimension_registry 全列 + aliases: [str, ...]}, ...]
    """
    dims = memory_dao.list_dimensions(mem_conn, active_only=active_only)
    result = [_dimension_with_aliases(mem_conn, d) for d in dims]
    return OperationResult.succeed({"total": len(result), "dimensions": result})


def registry_get(mem_conn: sqlite3.Connection, dim_id: str) -> OperationResult:
    """单维度详情（含别名）。

    未知维度 → OperationResult.fail(ERR_NOT_FOUND)（→ HTTP 404）。
    """
    dim = memory_dao.get_dimension(mem_conn, dim_id)
    if dim is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"维度不存在: {dim_id}")
    return OperationResult.succeed({"dimension": _dimension_with_aliases(mem_conn, dim)})


def registry_create_dim(
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    dim: dict[str, Any],
) -> OperationResult:
    """新增维度（幂等 upsert；重复提交更新字段，active 保留 DB 现值）。

    data 形态：{"status": "ok", "dimension": 归一化入参}——
    dimension 是**入参回显**（id/display_name/category/time_velocity/ttl_days/description/boundaries），
    非 DB 行（不含 active/created_at），HTTP 历史契约如此，勿改。

    Raises:
        InvalidArgs: id 非 snake_case / category / time_velocity 非法（→ HTTP 400）。
    """
    normalized = _normalize_dimension(dim)
    memory_dao.upsert_dimension(mem_conn, normalized)
    mem_conn.commit()
    # #33：写库后刷新 cfg['dimensions']（否则运行时新增维度不注入 L1 prompt）
    refresh_dimensions(cfg, mem_conn)
    logger.info("维度新增/更新: %s (%s)", normalized["id"], normalized["display_name"])
    return OperationResult.succeed({"status": "ok", "dimension": normalized})


def registry_update_dim(
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    dim_id: str,
    *,
    updates: dict[str, Any],
) -> OperationResult:
    """更新维度：停用/启用（active）、改名、TTL、描述。

    updates 键白名单由 data 层 ``update_dimension_fields`` 保证
    （active/display_name/ttl_days/description）。

    data 形态：{"status": "ok", "dimension_id": dim_id, "updates": 入参回显}。
    未知维度 → fail(ERR_NOT_FOUND)（→ HTTP 404）。
    """
    dim = memory_dao.get_dimension(mem_conn, dim_id)
    if dim is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"维度不存在: {dim_id}")
    memory_dao.update_dimension_fields(mem_conn, dim_id, updates)
    mem_conn.commit()
    # #33：写库后刷新 cfg['dimensions']（停用/启用即时生效）
    refresh_dimensions(cfg, mem_conn)
    return OperationResult.succeed({"status": "ok", "dimension_id": dim_id, "updates": updates})


def registry_create_alias(
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    alias: str,
    dimension_id: str,
) -> OperationResult:
    """新增别名（幂等 upsert）。

    data 形态：{"status": "ok", "alias": alias, "dimension_id": dimension_id}。
    目标维度不存在 → fail(ERR_NOT_FOUND)（→ HTTP 404）。
    """
    dim = memory_dao.get_dimension(mem_conn, dimension_id)
    if dim is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"维度不存在: {dimension_id}")
    memory_dao.upsert_alias(mem_conn, alias, dimension_id)
    mem_conn.commit()
    # #33：写库后刷新 cfg['dimensions']（别名虽不影响维度清单，统一走刷新保持语义一致）
    refresh_dimensions(cfg, mem_conn)
    return OperationResult.succeed({"status": "ok", "alias": alias, "dimension_id": dimension_id})


def registry_delete_alias(
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    alias: str,
) -> OperationResult:
    """删除别名。

    data 形态：{"status": "ok", "alias": alias, "deleted": 删除行数}。
    别名不存在（rowcount=0）→ fail(ERR_NOT_FOUND)（→ HTTP 404）。
    """
    cur = memory_dao.delete_alias(mem_conn, alias)
    mem_conn.commit()
    # #33：写库后刷新 cfg['dimensions']
    refresh_dimensions(cfg, mem_conn)
    if cur.rowcount == 0:
        return OperationResult.fail(ERR_NOT_FOUND, f"别名不存在: {alias}")
    return OperationResult.succeed({"status": "ok", "alias": alias, "deleted": cur.rowcount})


# ---------- 3. 投影函数（超集 → HTTP 历史契约形态） ----------

def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP ``/v1/admin/registry`` 各端点的历史契约形态。

    registry 链路改造前的响应体**本身就是**各操作返回的超集（无第二协议形态），
    本投影为恒等裁剪——它存在的意义是作为**契约形态的单一声明点**：
    契约等价性测试对照本模块冻结的 ``*_TOP_KEYS`` / ``DIMENSION_FIELD_KEYS``
    校验逐字段一致，后续若统一口径需先改这里。

    Returns:
        与输入相等的字典（不拷贝，避免无谓开销）。
    """
    return data
