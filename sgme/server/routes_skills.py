# -*- coding: utf-8 -*-
"""sgme/server/routes_skills.py：技能管理读侧 HTTP 端点（ST-36 M2 四级披露）。

扩展模块路由：skills.enabled=true 时由 server/app.py 挂载（镜像 care 模块开关
方式——禁用时整个 router 不注册，/v1/skills* 自然 404，核心零影响）。
鉴权：全部 require_agent_key（读侧是 agent 消费面，与记忆同权限级）。

与 /v1/admin/skills（routes_admin.py，skills_hub 写仓 CRUD + admin key）的边界：
- 写侧治理（put/remove/sync）归 admin 端点（另一工作流）；
- 本文件只做**读侧披露**——L0/L1/L2/L3 四级 + 统一搜索 skills 层的数据源端点。

端点：
- GET  /v1/skills                        L0 索引列表（budget 截断）
- GET  /v1/skills/{name}/digest          L1 摘要（frontmatter+骨架+uses）
- GET  /v1/skills/{name}?section=        L2 全文 / 节选
- POST /v1/skills/{name}/materialize     L3 字节保真落盘 {dest_dir}

业务逻辑全在 ``sgme.operations.skills``，本模块只做协议翻译（入口禁止写业务）。
索引缓存：app.state.skills_index 惰性建 BM25 索引对象 + rebuild_if_stale 复用
（百条规模重建毫秒级，缓存只为省重复扫描 IO；记录集变化自动失效重建）。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from sgme.server.app import api_error, require_agent_key, run_operation

router = APIRouter()


class MaterializeRequest(BaseModel):
    """L3 物化请求体：目标目录（agent 工作区）。"""

    dest_dir: str


def _ensure_index(request: Request):
    """惰性建/复用 app.state.skills_bm25 索引对象（rebuild_if_stale 判过期）。

    - 记录集现扫一次（git 工作区 ∪ wiki skill 页），与上次内容 SHA/数量一致
      则复用既有 SkillsBm25 对象，变化才整表重建（百条规模毫秒级）；
    - L0 列表端点消费 len(records)；L1/L2/L3 仍走 operations 层现查
      （单技能取回无需全量索引）。
    """
    from sgme.skills.bm25 import rebuild_if_stale
    from sgme.skills.config import parse_skills_config
    from sgme.skills.indexer import index_all

    cfg = request.app.state.cfg
    sc = parse_skills_config(cfg)
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    records = index_all(sc.source_dirs, wiki_conn)[: sc.budget]
    cached = getattr(request.app.state, "skills_bm25", None)
    index = rebuild_if_stale(cached, records)
    request.app.state.skills_bm25 = index  # 写回（新索引或原对象）
    return records


# ---------- GET /v1/skills （L0 索引列表） ----------

@router.get("/v1/skills")
def list_skills(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1),
    _: str = Depends(require_agent_key),
):
    """L0 索引列表：name/description/category/tags（受 budget 截断）。

    v0.7 规范：业务下沉 operations.skills，本函数只做协议翻译。
    """
    from sgme.operations.skills import list_skills as list_skills_operation

    cfg = request.app.state.cfg
    _ensure_index(request)  # 惰性建/复用 BM25 索引缓存（app.state.skills_bm25）
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    # T-112：库可用时走 skills.db（结构化查询 + 免全量扫描），None 时操作层回退内存索引
    skills_conn: sqlite3.Connection | None = getattr(request.app.state, "skills_conn", None)
    return run_operation(list_skills_operation, cfg, wiki_conn,
                        offset=offset, limit=limit, skills_conn=skills_conn)


# ---------- GET /v1/skills/coldstart （冷启动包，须先于 /{name} 注册） ----------

@router.get("/v1/skills/coldstart")
def skills_coldstart(
    request: Request,
    _: str = Depends(require_agent_key),
):
    """冷启动包：索引全量 + 热集（pattern=auto）全文 + SGME 操作手册。

    新 agent 一次拉取即刻可用（设计 §三「冷启动包」）；须注册在 ``/{name}``
    动态路由之前，否则 ``coldstart`` 会被当成技能名命中 L2 端点。
    """
    from sgme.operations.skills import cold_start as cold_start_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    return run_operation(cold_start_operation, cfg, wiki_conn)

# ---------- GET /v1/skills/{name}/digest （L1 摘要） ----------

@router.get("/v1/skills/{name}/digest")
def skill_digest(
    name: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """L1 摘要：frontmatter 字段 + 正文骨架 + uses 清单。审核媒介层。"""
    from sgme.operations.skills import skill_digest as digest_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    data = run_operation(digest_operation, cfg, wiki_conn, name=name)
    # L1 附带可用小节骨架的便捷提示（sections 已含标题行，无需额外字段）
    return data


# ---------- GET /v1/skills/{name}?section= （L2 全文/节选） ----------

@router.get("/v1/skills/{name}")
def skill_get(
    name: str,
    request: Request,
    section: str | None = None,
    _: str = Depends(require_agent_key),
):
    """L2 全文：正文全文注入；section 给定时截取该节（省 token）。"""
    from sgme.operations.skills import skill_get as get_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    return run_operation(get_operation, cfg, wiki_conn, name=name, section=section)


# ---------- POST /v1/skills/{name}/materialize （L3 物化落盘） ----------

@router.post("/v1/skills/{name}/materialize")
def materialize_skill(
    name: str,
    payload: MaterializeRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """L3 物化：SKILL.md 原文件字节写盘 dest_dir/<name>/SKILL.md。

    字节保真铁律：不走 LLM 转写；成功记遥测日志一条（name/sha/ts）。
    dest_dir 缺失 → pydantic 422（框架层把关，镜像 idea_add 必填语义）。
    """
    from sgme.operations.skills import materialize as materialize_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    return run_operation(
        materialize_operation, cfg, wiki_conn,
        name=name, dest_dir=payload.dest_dir,
    )
