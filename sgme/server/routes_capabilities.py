"""server/routes_capabilities.py：GET /v1/capabilities（D-58 适配器能力广告）。

适配器启动时读它判断契约是否匹配：不匹配只打日志提示、不阻塞（单用户场景不做强制
弹窗）。响应三字段是设计要求的最小集：schema_version / api_compat / catalog_version，
其余（兼容期窗口、409/410 端点清单、强制开关状态）为只增字段。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from sgme import compat as compat_mod
from sgme.server.app import require_agent_key

router = APIRouter()


@router.get("/v1/capabilities")
def capabilities(request: Request, _: str = Depends(require_agent_key)) -> dict:
    """服务能力广告（Agent Key 可读；只读、无副作用）。"""
    state = request.app.state
    return compat_mod.capabilities(
        state.mem_conn, state.cfg, getattr(state, "skills_conn", None)
    )
