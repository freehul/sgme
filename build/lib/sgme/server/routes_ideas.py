"""server/routes_ideas.py：创意池管理端点（0.8 ST-14，WebUI ST-7 前置接线）。

业务实现全部在 ``sgme.operations.idea``，本文件只做协议翻译
（v0.7 §7 分层：入口层只取依赖 → 调 operation → run_operation 统一翻错误码）。

端点（对齐 operations/idea.py docstring 的 HTTP 映射）：
    GET    /v1/admin/ideas                创意列表分页
    GET    /v1/admin/ideas/{idea_id}    单条创意详情
    PATCH  /v1/admin/ideas/{idea_id}    编辑（content / priority）
    POST   /v1/admin/ideas/{idea_id}/notes   追加式备注
    PUT    /v1/admin/ideas/{idea_id}/flag    设置/清除人工标记
    DELETE /v1/admin/ideas/{idea_id}    软删除（status='rejected'，可恢复）
    POST   /v1/admin/ideas/{idea_id}/restore 恢复软删除

请求体用**裸 dict**（对齐 routes_admin.update_scene_status / update_project 先例）：
PATCH / PUT 必须区分「未传键（保持原值）」与「显式 null（清除/解绑）」，
dict 的键存在性天然表达这一语义。

鉴权：全部 Depends(require_admin_key)（403 缺失/非管理员，401 Bearer 缺失）。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request

from sgme.operations.idea import (
    add_idea as add_idea_operation,
    append_note as append_note_operation,
    get_idea as get_idea_operation,
    list_ideas as list_ideas_operation,
    promote_idea as promote_idea_operation,
    restore_idea as restore_idea_operation,
    set_flag as set_flag_operation,
    soft_delete_idea as soft_delete_idea_operation,
    update_idea as update_idea_operation,
)
from sgme.server.app import require_admin_key, run_operation

router = APIRouter()


@router.get("/v1/admin/ideas")
def list_ideas(
    request: Request,
    page: str | None = None,
    limit: str | None = None,
    status: str | None = None,
    custom_flag: str | None = None,
    has_flag: str | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    _: str = Depends(require_admin_key),
):
    """创意列表分页（纯只读，ideas 独立表 T-56）。

    请求 Query：一律以 ``str | None`` 收入（对齐 routes_admin F1 端点惯例），
    解析/校验下沉 operations 层（InvalidArgs → 400 ERR_INVALID_ARGS）。
    默认仅 active；``status=all`` = 不过滤状态。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        list_ideas_operation,
        mem_conn,
        page=page,
        limit=limit,
        status=status,
        custom_flag=custom_flag,
        has_flag=has_flag,
        q=q,
        since=since,
        until=until,
        sort=sort,
        order=order,
    )


@router.post("/v1/admin/ideas")
def create_idea(
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """人工添加创意（2026-08-13 用户定：创意由用户主动提出，不再 LLM 自动打标）。

    Body：``content``（必填非空）/ ``priority``（0-100，缺省 50）/ ``source_ref``（可选溯源）。
    自动打 ``ideas`` 标签 + ``ttl_days=NULL``（创意长期保存铁律）。
    """
    b = body or {}
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        add_idea_operation,
        mem_conn,
        content=b.get("content"),
        priority=b.get("priority"),
        source_ref=b.get("source_ref"),
    )


@router.get("/v1/admin/ideas/{idea_id}")
def get_idea(
    idea_id: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """单条创意详情；不存在 → 404 ERR_NOT_FOUND。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(get_idea_operation, mem_conn, idea_id)


@router.patch("/v1/admin/ideas/{idea_id}")
def update_idea(
    idea_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """编辑创意内容 / 优先级。

    Body：``content`` / ``priority``（0-100），只改传入字段。
    两个字段都没给 → 400 ERR_INVALID_ARGS；不存在 → 404。
    """
    b = body or {}
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        update_idea_operation,
        mem_conn,
        idea_id,
        content=b.get("content"),
        priority=b.get("priority"),
    )


@router.post("/v1/admin/ideas/{idea_id}/notes")
def append_note(
    idea_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """追加式备注（绝不覆盖既有备注）。Body：``text``（必填，非空）。"""
    b = body or {}
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        append_note_operation,
        mem_conn,
        idea_id,
        text=b.get("text"),
    )


@router.put("/v1/admin/ideas/{idea_id}/flag")
def set_flag(
    idea_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """设置 / 清除人工标记（自由文本，无枚举校验）。

    Body：``custom_flag``（标记文本；空串或 null = 清除标记）。
    """
    b = body or {}
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        set_flag_operation,
        mem_conn,
        idea_id,
        custom_flag=b.get("custom_flag"),
    )


@router.delete("/v1/admin/ideas/{idea_id}")
def soft_delete_idea(
    idea_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """软删除创意（status='rejected'，绝不物理删除，可恢复）。

    Body：``reason``（可选删除说明）。
    """
    b = body or {}
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        soft_delete_idea_operation,
        mem_conn,
        idea_id,
        reason=b.get("reason"),
    )


@router.post("/v1/admin/ideas/{idea_id}/restore")
def restore_idea(
    idea_id: str,
    request: Request,
    _: str = Depends(require_admin_key),
):
    """撤销软删除：恢复 status='active'。"""
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(restore_idea_operation, mem_conn, idea_id)


@router.post("/v1/admin/ideas/{idea_id}/promote")
def promote_idea(
    idea_id: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """创意升格为需求（联动：置 promoted 标记 + 创建 demand 回填 origin_idea_id）。

    Body：``title``（必填）/ ``content`` / ``priority`` / ``project_id`` / ``source_ref``。
    不存在 → 404；title 缺失 → 400；demand 创建失败错误透传。
    """
    b = body or {}
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    return run_operation(
        promote_idea_operation,
        mem_conn,
        idea_id,
        title=b.get("title"),
        content=b.get("content"),
        priority=b.get("priority"),
        project_id=b.get("project_id"),
        source_ref=b.get("source_ref"),
    )