"""routes_registry.py：维度注册表管理 API（契约 §5：GET /v1/admin/registry）。

设计（§8.1 维度可扩展决策）：
- 用户 UI / SCSM 经此接口动态新增维度（注册表加行 + 别名打标即可，无需 schema 迁移）
- 维度 id 必须是 snake_case（防注入/防不规范键）
- 停用（active=false）而非删除——历史记忆的维度标签保留可溯源
- 仅管理员 Key 可调

0.8 T-8 重构后（v0.7 §7）：业务逻辑全部下沉到 ``sgme.operations.registry``，
本路由退化为**纯协议翻译**薄壳：鉴权 → 参数解析（Pydantic）→ ``run_operation``
（统一翻译错误码）→ ``http_payload`` 投影响应。响应格式与改造前逐字段一致
（契约等价性由 tests/test_operations_registry.py 冻结校验）。

兼容性：``refresh_dimensions`` 自 v0.6 起定义于本模块并被外部引用
（tests/test_prompts_qa_acceptance.py 直接 import），故在此原样 re-export，
调用方零改动；实现已随业务下沉到 operations 层。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from sgme.operations.registry import (
    http_payload as registry_http_payload,
)
from sgme.operations.registry import (
    refresh_dimensions,
    registry_create_alias,
    registry_create_dim,
    registry_delete_alias,
    registry_get,
    registry_list,
    registry_update_dim,
)
from sgme.data import memory_dao
from sgme import config as sgme_config
from sgme.server.app import require_admin_key, run_operation

router = APIRouter(prefix="/v1/admin/registry", tags=["registry"])


class DimensionCreateRequest(BaseModel):
    """新增维度请求。"""

    id: str = Field(..., description="维度 id（snake_case，1-32 字符）")
    display_name: str = Field(..., min_length=1, max_length=50, description="中文展示名")
    category: str = Field(default="动态", description="静态 | 偏好 | 动态")
    time_velocity: str = Field(default="dynamic", description="static | dynamic")
    ttl_days: int | None = Field(default=None, ge=1, le=3650, description="动态维度默认 TTL")
    description: str = Field(default="", max_length=500, description="边界定义")


class DimensionUpdateRequest(BaseModel):
    """更新维度（停用/启用/改名等）。"""

    active: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=50)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)
    description: str | None = Field(default=None, max_length=500)


class AliasRequest(BaseModel):
    """新增别名请求。"""

    alias: str = Field(..., min_length=1, max_length=50, description="同义表述")
    dimension_id: str = Field(..., description="目标维度 id")


# ---------- 端点（纯协议翻译：参数解析 → run_operation → http_payload 投影） ----------

@router.get("")
def list_registry(request: Request, _: str = Depends(require_admin_key), active_only: bool = True):
    """列出全部维度（含别名）。active_only=false 时含停用维度。"""
    mem_conn = request.app.state.mem_conn
    data = run_operation(registry_list, mem_conn, active_only=active_only)
    return registry_http_payload(data)


@router.get("/consistency")
def check_registry_consistency(request: Request, _: str = Depends(require_admin_key)):
    """维度注册表一致性诊断（T-128）：DB active 集 vs registry/dimensions.yaml。

    返回 check_dimension_consistency 报告：consistent 布尔 + 三类差集
    （orphan_active_in_db 应禁用 / missing_in_db 应导入 / inactive_in_db 应启用）。
    不一致即意味着脏数据风险，应告警 + 治理。
    """
    mem_conn = request.app.state.mem_conn
    yaml_dim_ids = request.app.state.cfg.get("_yaml_dimension_ids")
    if not yaml_dim_ids:
        # 兜底：启动期未初始化时直接读 YAML（覆盖异常部署情形）
        yaml_dim_ids = {d["id"] for d in sgme_config.load_dimensions()}
    return memory_dao.check_dimension_consistency(mem_conn, set(yaml_dim_ids))


@router.get("/{dim_id}")
def get_registry_dimension(dim_id: str, request: Request, _: str = Depends(require_admin_key)):
    """单维度详情（含别名）；未知维度 → 404。"""
    mem_conn = request.app.state.mem_conn
    data = run_operation(registry_get, mem_conn, dim_id)
    return registry_http_payload(data)


@router.post("/dimensions")
def create_dimension(payload: DimensionCreateRequest, request: Request,
                     _: str = Depends(require_admin_key)):
    """新增维度（幂等 upsert；重复提交更新字段）；非法 id/category → 400。"""
    mem_conn = request.app.state.mem_conn
    data = run_operation(
        registry_create_dim, mem_conn, request.app.state.cfg,
        dim=payload.model_dump(),
    )
    return registry_http_payload(data)


@router.put("/dimensions/{dim_id}")
def update_dimension(dim_id: str, payload: DimensionUpdateRequest, request: Request,
                     _: str = Depends(require_admin_key)):
    """更新维度：停用/启用（active）、改名、TTL、描述；未知维度 → 404。"""
    mem_conn = request.app.state.mem_conn
    data = run_operation(
        registry_update_dim, mem_conn, request.app.state.cfg, dim_id,
        updates=payload.model_dump(exclude_unset=True),
    )
    return registry_http_payload(data)


@router.post("/aliases")
def add_alias(payload: AliasRequest, request: Request, _: str = Depends(require_admin_key)):
    """新增别名（幂等 upsert）；目标维度不存在 → 404。"""
    mem_conn = request.app.state.mem_conn
    data = run_operation(
        registry_create_alias, mem_conn, request.app.state.cfg,
        alias=payload.alias, dimension_id=payload.dimension_id,
    )
    return registry_http_payload(data)


@router.delete("/aliases/{alias}")
def remove_alias(alias: str, request: Request, _: str = Depends(require_admin_key)):
    """删除别名；别名不存在 → 404。"""
    mem_conn = request.app.state.mem_conn
    data = run_operation(registry_delete_alias, mem_conn, request.app.state.cfg, alias)
    return registry_http_payload(data)
