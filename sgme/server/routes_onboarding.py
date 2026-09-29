# -*- coding: utf-8 -*-
"""SGME 在线接入文档端点（T-231，v1.7.2）：/v1/onboarding/docs。

背景（2026-09-30 审读）：health.onboarding 只指向仓库相对路径，且 AI-INSTALL 未随
镜像分发、无在线端点——纯远程 agent（本机无仓库）拿不到接入指引正文。本端点把
``AI-INSTALL/`` 目录内容经 HTTP 只读暴露：

- ``GET /v1/onboarding/docs``          文档索引（名称 / 标题 / 字节数 / 在线路径）
- ``GET /v1/onboarding/docs/{name}``   单文档全文（markdown，name 支持 ``prompts/`` 子目录）

安全与边界：
- ``name`` 走**白名单**（精确匹配相对路径），非法即 404——不存在路径穿越面；
- 只读静态响应，无 DB / 无写路径；与 ``/v1/health`` 同为**免 Key**公开面
  （内容与公开仓库一致，且不涉任何密钥）；
- 文档根解析顺序：``SGME_AI_INSTALL_DIR`` 环境变量 → 包上级 ``AI-INSTALL/``
  （源码运行 = 仓库根；容器 = ``/app/AI-INSTALL``，由 Dockerfile COPY 内置）。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

router = APIRouter(tags=["onboarding"])

# 白名单：AI-INSTALL 下允许在线阅读的文档（相对路径 → 标题）
_DOCS: dict[str, str] = {
    "README.md": "AI-INSTALL 总览与快速开始",
    "agent-onboarding.md": "Agent Onboarding 指引（协议唯一真相）",
    "selfcheck.md": "接入后自检清单（八项）",
    "免费模型Key申请指南.md": "免费模型 Key 申请指南",
    "prompts/01-install.md": "任务卡 ① 部署 / 发现 SGME",
    "prompts/02-connect.md": "任务卡 ② 接入与验证",
    "prompts/03-init-agent-files.md": "任务卡 ③ 初始化本地文件",
    "prompts/04-daily-loop.md": "任务卡 ④ 日常使用循环",
}

_MISSING_ROOT_MSG = (
    "AI-INSTALL 文档未随本部署分发：请设置 SGME_AI_INSTALL_DIR，"
    "或使用含 AI-INSTALL/ 的镜像（v1.7.2+）"
)


def _docs_root() -> Path | None:
    """定位 AI-INSTALL 目录（首个命中的候选；都不存在返回 None）。"""
    candidates: list[Path] = []
    env_dir = os.environ.get("SGME_AI_INSTALL_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    # 包上级：<repo|/app>/AI-INSTALL（sgme/server/routes_onboarding.py → parents[2]）
    candidates.append(Path(__file__).resolve().parents[2] / "AI-INSTALL")
    candidates.append(Path("/app/AI-INSTALL"))
    for cand in candidates:
        if (cand / "agent-onboarding.md").is_file():
            return cand
    return None


@router.get("/v1/onboarding/docs")
def onboarding_docs_index():
    """AI-INSTALL 在线索引（免 Key）。"""
    root = _docs_root()
    if root is None:
        raise HTTPException(status_code=503, detail=_MISSING_ROOT_MSG)
    items = []
    for name, title in _DOCS.items():
        p = root / name
        if p.is_file():
            items.append({
                "name": name,
                "title": title,
                "path": f"/v1/onboarding/docs/{name}",
                "bytes": p.stat().st_size,
            })
    return {
        "count": len(items),
        "docs": items,
        "hint": "全文 GET /v1/onboarding/docs/{name}；本端点与 /v1/health 同级、免 Key；"
                "本地含仓库时也可直接读 AI-INSTALL/（同内容）",
    }


@router.get("/v1/onboarding/docs/{name:path}")
def onboarding_doc(name: str):
    """单文档全文（markdown；name 走白名单，支持 prompts/ 子目录）。"""
    if name not in _DOCS:
        raise HTTPException(
            status_code=404,
            detail=f"未知文档: {name}；索引见 GET /v1/onboarding/docs",
        )
    root = _docs_root()
    if root is None:
        raise HTTPException(status_code=503, detail=_MISSING_ROOT_MSG)
    p = root / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"文档未随本部署分发: {name}")
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )
