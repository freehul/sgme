"""server/routes_prompts.py：提示词版本管理 Admin API（#33）——纯协议翻译层。

- GET  /v1/admin/prompts               列出全部 stage：active / ab / versions
- POST /v1/admin/prompts/publish       发布新版本（工作副本 → vNNN）
- POST /v1/admin/prompts/activate      激活版本（@working / vNNN）
- POST /v1/admin/prompts/ab            配置 A/B 分流（enabled=false 关闭）
- GET  /v1/admin/prompts/metrics       A/B 观测汇总（refine_runs + memories）

v0.8 T-8 重构后：业务逻辑全部下沉 ``sgme.operations.prompts``（版本管理编排 /
错误翻译 / 观测汇总），本模块退化为薄壳：鉴权 → Pydantic 请求模型解析 →
``run_operation`` → ``http_payload_*`` 投影。响应格式与改造前逐字段一致
（契约等价性由 tests/test_operations_prompts.py 冻结校验）。

鉴权：沿用 admin key（与 routes_config/routes_registry 一致，设计 §6 #6）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from sgme.operations.prompts import (
    http_payload_ab,
    http_payload_activate,
    http_payload_list,
    http_payload_metrics,
    http_payload_publish,
    prompts_ab,
    prompts_activate,
    prompts_list,
    prompts_metrics,
    prompts_publish,
)
from sgme.server.app import require_admin_key, run_operation

router = APIRouter(prefix="/v1/admin/prompts", tags=["prompts"])


# ---------- 请求模型 ----------

class PublishRequest(BaseModel):
    stage: str = Field(..., description="stage 名")
    note: str = Field(default="", max_length=200, description="发布说明")


class ActivateRequest(BaseModel):
    stage: str = Field(..., description="stage 名")
    version_ref: str = Field(..., description="@working / vNNN / versions/<stage>/vNNN.txt")


class ABRequest(BaseModel):
    stage: str = Field(..., description="stage 名")
    a: str | None = Field(default=None, description="A 版本引用")
    b: str | None = Field(default=None, description="B 版本引用")
    split: float = Field(default=0.5, ge=0.0, le=1.0, description="A 流量占比")
    bucket_by: str = Field(default="file_id", description="file_id | memory_id | random")
    enabled: bool = Field(default=True, description="false 时关闭 A/B")


# ---------- GET /v1/admin/prompts ----------

@router.get("")
def list_prompts(request: Request, _: str = Depends(require_admin_key)):
    """列出全部 stage 的 active / ab / versions。"""
    data = run_operation(prompts_list)
    return http_payload_list(data)


# ---------- POST /v1/admin/prompts/publish ----------

@router.post("/publish")
def publish(payload: PublishRequest, request: Request, _: str = Depends(require_admin_key)):
    """发布新版本：工作副本 → versions/<stage>/vNNN.txt（原子写）。"""
    data = run_operation(prompts_publish, payload.stage, note=payload.note)
    return http_payload_publish(data)


# ---------- POST /v1/admin/prompts/activate ----------

@router.post("/activate")
def activate(payload: ActivateRequest, request: Request, _: str = Depends(require_admin_key)):
    """激活版本：@working（热更新）或钉版 vNNN。"""
    data = run_operation(prompts_activate, payload.stage, payload.version_ref)
    return http_payload_activate(data)


# ---------- POST /v1/admin/prompts/ab ----------

@router.post("/ab")
def configure_ab(payload: ABRequest, request: Request, _: str = Depends(require_admin_key)):
    """配置 A/B 分流（enabled=false 关闭，下次渲染起回落 active 指向）。"""
    data = run_operation(
        prompts_ab, payload.stage,
        a=payload.a, b=payload.b, split=payload.split,
        bucket_by=payload.bucket_by, enabled=payload.enabled,
    )
    return http_payload_ab(data)


# ---------- GET /v1/admin/prompts/metrics ----------

@router.get("/metrics")
def metrics(
    request: Request,
    stage: str = Query(..., description="stage 名"),
    since: str | None = Query(None, description="仅统计 started_at >= since 的 run"),
    _: str = Depends(require_admin_key),
):
    """A/B 观测汇总：按 (version, variant) 分组 runs / error / memories / avg(priority) / action 分布。

    不做自动裁决（结论留人工 + 评测集 #32）。
    """
    data = run_operation(
        prompts_metrics, request.app.state.mem_conn, stage=stage, since=since,
    )
    return http_payload_metrics(data)
