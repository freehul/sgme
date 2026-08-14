"""routes_config.py：配置管理 API（SCSM 远程读写 SGME 配置）。

设计要点（2026-08-04 用户决策）：
- SCSM 不直接改 NAS 上的 SGME 配置文件（跨机无法直接写），
  改由 SGME Server 进程提供 HTTP 接口读写自身配置（sgme.yaml）。
- GET  /v1/admin/config            → 返回当前生效配置（含默认值合并）
- GET  /v1/admin/config/{section}  → 返回单个配置段（l1/l2/refine/search/backup）
- PUT  /v1/admin/config            → 更新配置段（部分更新，合并后落盘）
- 仅管理员 Key 可调

v0.7 §7 重构后：读写/校验/落盘编排全部下沉到 ``sgme.operations.config``
（它再往下只调 ``sgme.config`` 这个配置唯一读写方），本路由退化为薄壳：
鉴权 → 参数解析 → ``run_operation`` → 投影函数裁剪响应。

⚠️ 命名雷区：本文件同时用到 ``sgme.config``（配置层）与
``sgme.operations.config``（操作层）。为免撞名，操作层一律按
``from sgme.operations.config import <op> as config_<op>`` 的别名形式导入。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from sgme import config as sgme_config
from sgme.operations.config import get_config as get_config_operation
from sgme.operations.config import get_config_section as get_config_section_operation
from sgme.operations.config import get_http_payload as config_get_http_payload
from sgme.operations.config import update_config as update_config_operation
from sgme.operations.config import update_payload as config_update_payload
from sgme.server.app import require_admin_key, run_operation

router = APIRouter(prefix="/v1/admin/config", tags=["config"])

# 可写段白名单（权威源在 sgme.config.CONFIG_SECTIONS）
# 保留该别名：v0.6 起就是本模块的公开常量，测试与外部代码有引用。
CONFIG_SECTIONS = sgme_config.CONFIG_SECTIONS


class ConfigUpdateRequest(BaseModel):
    """配置更新请求：{section: {key: value}} 或 {key: value}（单段）。"""

    section: str | None = Field(default=None, description="配置段名；None 表示请求体本身就是单段内容")
    values: Dict[str, Any] = Field(default_factory=dict, description="要更新的键值")


@router.get("")
def get_config(request: Request, _: str = Depends(require_admin_key)):
    """返回当前生效配置（含默认值合并）。"""
    cfg = request.app.state.cfg
    data = run_operation(get_config_operation, cfg)
    return config_get_http_payload(data)


@router.get("/{section}")
def get_config_section(section: str, request: Request, _: str = Depends(require_admin_key)):
    """返回单个配置段（未知段 → 404 ERR_NOT_FOUND）。"""
    cfg = request.app.state.cfg
    data = run_operation(get_config_section_operation, cfg, section=section)
    return config_get_http_payload(data)


@router.put("")
def update_config(payload: ConfigUpdateRequest, request: Request, _: str = Depends(require_admin_key)):
    """更新配置段（部分更新，合并后落盘 sgme.yaml，热生效）。

    请求体两种形态：
      {"section": "refine", "values": {"refine_on_append": true}}
      {"section": null, "values": {"batch_scan": {"enabled": false}}}  # 单段（键=段名）
    """
    return _do_update_config(payload, request)


@router.post("")
def update_config_post(payload: ConfigUpdateRequest, request: Request,
                       _: str = Depends(require_admin_key)):
    """契约 §5 兼容：POST /v1/admin/config 等价 PUT（更新配置）。"""
    return _do_update_config(payload, request)


def _do_update_config(payload: ConfigUpdateRequest, request: Request) -> dict:
    """配置更新公共逻辑（PUT/POST 共用）——薄壳：编排全部委托 operations 层。

    ``request.app.state.cfg = cfg`` 的回写保留 v0.6 写法：cfg 是就地修改的
    同一个字典对象，这行实际是幂等的，但保留可避免未来换成不可变配置时漏改。
    """
    cfg = request.app.state.cfg
    data = run_operation(
        update_config_operation, cfg, section=payload.section, values=payload.values,
    )
    request.app.state.cfg = cfg
    return config_update_payload(data)
