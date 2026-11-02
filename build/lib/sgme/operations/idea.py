"""operations/idea.py：创意池人工修正操作（0.8 ST-14，v0.7 §7 多操作模块）。

承接的入口
----------
========================================  ==========================================
入口                                       操作
========================================  ==========================================
HTTP ``GET    /v1/admin/ideas``            ``list_ideas``
HTTP ``GET    /v1/admin/ideas/{id}``       ``get_idea``
HTTP ``PATCH  /v1/admin/ideas/{id}``       ``update_idea``
HTTP ``POST   /v1/admin/ideas/{id}/notes`` ``append_note``
HTTP ``PUT    /v1/admin/ideas/{id}/flag``  ``set_flag``
HTTP ``DELETE /v1/admin/ideas/{id}``       ``soft_delete_idea``
HTTP ``POST   /v1/admin/ideas/{id}/restore`` ``restore_idea``
========================================  ==========================================

无 MCP 入口 → 不写投影函数（``operations/__init__.py``：两端形态一致时 data 即响应）。

分层铁律（照抄 health.py / memory.py 样板）
------------------------------------------
- 本模块**不认识协议**：不 import fastapi，不知道 HTTP 状态码；
  依赖（mem_conn / 业务参数）由入口层显式传入。
- 参数校验抛 ``InvalidArgs``（入口层 ``run_operation`` 翻译为 400 ERR_INVALID_ARGS）；
  资源不存在返回 ``OperationResult.fail(ERR_NOT_FOUND, ...)`` → 404。
- **不加 catch-all except**：非预期异常原样上抛，由入口层全局处理器兜底。
- data 顶层**不得**使用 ``error`` 键（MCP 入口以该键判定失败态）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from sgme.data import idea_dao
from sgme.operations.errors import ERR_INTERNAL, ERR_NOT_FOUND, InvalidArgs, OperationResult

# 分页默认与硬上限（对齐接口契约 §5.3.2：limit 1-200 默认 50，page ≥ 1 默认 1）
DEFAULT_PAGE: int = 1
DEFAULT_LIMIT: int = 50
MAX_LIMIT: int = 200

# 备注 / 标记的长度上限（防超长文本撑爆单行；非契约字段，取保守值）
MAX_NOTE_LEN: int = 4000
MAX_FLAG_LEN: int = 200
MAX_CONTENT_LEN: int = 20000

# 排序 / 顺序白名单
VALID_SORTS: tuple[str, ...] = tuple(idea_dao.SORT_COLUMNS.keys())
VALID_ORDERS: tuple[str, ...] = ("asc", "desc")

# 列表端点的缺省状态过滤：仅 active（软删除条目显式传 status 才可见）
DEFAULT_STATUSES: tuple[str, ...] = ("active",)


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（与仓库既有 `_now_iso` 完全一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _not_found(idea_id: str) -> OperationResult:
    """构造「创意不存在」失败结果（404 ERR_NOT_FOUND）。"""
    return OperationResult.fail(ERR_NOT_FOUND, f"创意不存在: {idea_id}")


# ---------- 参数解析（照抄 routes_admin._parse_active_within 惯例：非法即 400） ----------

def parse_page(raw: str | int | None) -> int:
    """解析 `page`：缺省 1，必须是 ≥ 1 的整数。

    Raises:
        InvalidArgs: 非整数或 < 1。
    """
    if raw is None or raw == "":
        return DEFAULT_PAGE
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise InvalidArgs(f"page 必须是整数: {raw}") from None
    if val < 1:
        raise InvalidArgs(f"page 必须 ≥ 1: {raw}")
    return val


def parse_limit(raw: str | int | None) -> int:
    """解析 `limit`：缺省 50，取值 1-200（**上限硬限制 200**，防查询放大）。

    Raises:
        InvalidArgs: 非整数、< 1 或 > 200。
    """
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise InvalidArgs(f"limit 必须是整数: {raw}") from None
    if val < 1:
        raise InvalidArgs(f"limit 必须 ≥ 1: {raw}")
    if val > MAX_LIMIT:
        raise InvalidArgs(f"limit 上限 {MAX_LIMIT}: {raw}")
    return val


def parse_sort(raw: str | None) -> str:
    """解析 `sort`：缺省 updated_at，须在白名单内。"""
    if raw is None or raw == "":
        return "updated_at"
    if raw not in VALID_SORTS:
        raise InvalidArgs(f"未知 sort: {raw}（可选 {list(VALID_SORTS)}）")
    return raw


def parse_order(raw: str | None) -> str:
    """解析 `order`：缺省 desc，须为 asc / desc（大小写不敏感）。"""
    if raw is None or raw == "":
        return "desc"
    val = str(raw).lower()
    if val not in VALID_ORDERS:
        raise InvalidArgs(f"未知 order: {raw}（可选 {list(VALID_ORDERS)}）")
    return val


def parse_statuses(raw: str | None) -> tuple[str, ...]:
    """解析 `status`：逗号分隔多值，缺省仅 active；`all` 表示不过滤状态。

    Raises:
        InvalidArgs: 出现枚举外的状态值。
    """
    if raw is None or raw == "":
        return DEFAULT_STATUSES
    if str(raw).strip().lower() == "all":
        return ()
    values = [s.strip() for s in str(raw).split(",") if s.strip()]
    if not values:
        return DEFAULT_STATUSES
    for v in values:
        if v not in idea_dao.VALID_STATUSES:
            raise InvalidArgs(
                f"未知 status: {v}（可选 {sorted(idea_dao.VALID_STATUSES)} 或 all）"
            )
    return tuple(values)


def parse_has_flag(raw: str | bool | None) -> bool | None:
    """解析 `has_flag`：true/false（大小写不敏感）；缺省 None = 不过滤。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    val = str(raw).strip().lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    raise InvalidArgs(f"has_flag 必须是 true / false: {raw}")


# ---------- 操作 1：创意列表分页 ----------

def list_ideas(
    mem_conn: sqlite3.Connection,
    *,
    page: str | int | None = None,
    limit: str | int | None = None,
    status: str | None = None,
    custom_flag: str | None = None,
    has_flag: str | bool | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> OperationResult:
    """创意列表分页（ideas 独立表，T-56：默认仅 active）。

    Args:
        mem_conn: memory.db 连接。
        page / limit: 分页参数（原始串，本函数负责校验）。
        status: 逗号分隔状态白名单；`all` = 不过滤。
        custom_flag: 人工标记精确匹配。
        has_flag: true 仅有标记 / false 仅无标记。
        q: 内容子串。
        since / until: 时间窗（作用于 sort 时间列）。
        sort / order: 排序字段与方向。

    Returns:
        成功时 data 为 ``{items, count, total, page, limit, generated_at}``
        （信封与既有 admin 列表端点一致）。
    """
    p = parse_page(page)
    lim = parse_limit(limit)
    srt = parse_sort(sort)
    ordr = parse_order(order)
    statuses = parse_statuses(status)
    flag_filter = parse_has_flag(has_flag)

    common: dict[str, Any] = {
        "statuses": statuses,
        "custom_flag": custom_flag,
        "has_flag": flag_filter,
        "q": q,
        "since": since,
        "until": until,
        "sort": srt,
    }
    total = idea_dao.count_ideas(mem_conn, **common)
    items = idea_dao.list_ideas(mem_conn, order=ordr, page=p, limit=lim, **common)
    return OperationResult.succeed({
        "items": items,
        "count": len(items),
        "total": total,
        "page": p,
        "limit": lim,
        "generated_at": _now_iso(),
    })


# ---------- 操作 2：读取单条创意 ----------

def get_idea(
    mem_conn: sqlite3.Connection,
    idea_id: str,
) -> OperationResult:
    """读取单条创意详情。

    Args:
        mem_conn: memory.db 连接。
        idea_id: 创意 id。

    Returns:
        成功时 data 为 ``{"idea": {...}}``。
    """
    idea = idea_dao.get_idea(mem_conn, idea_id)
    if idea is None:
        return _not_found(idea_id)
    return OperationResult.succeed({"idea": idea})


# ---------- 操作 3：编辑创意（可修改） ----------

def update_idea(
    mem_conn: sqlite3.Connection,
    idea_id: str,
    *,
    content: str | None = None,
    priority: int | None = None,
) -> OperationResult:
    """编辑创意内容 / 优先级（人工完善是创意池核心环节，设计 §5）。

    Args:
        mem_conn: memory.db 连接。
        idea_id: 创意 id。
        content: 新内容；None 表示不改。
        priority: 新优先级 0-100；None 表示不改。

    Returns:
        成功时 data 为 ``{"idea": {...}, "updated_fields": [...]}``。

    Raises:
        InvalidArgs: 两个字段都没给 / content 空串 / priority 越界。
    """
    if content is None and priority is None:
        raise InvalidArgs("至少需提供 content 或 priority 之一")
    if content is not None:
        if not isinstance(content, str) or not content.strip():
            raise InvalidArgs("content 不能为空")
        if len(content) > MAX_CONTENT_LEN:
            raise InvalidArgs(f"content 超长（上限 {MAX_CONTENT_LEN} 字符）")
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            raise InvalidArgs(f"priority 必须是整数: {priority}") from None
        if not 0 <= priority <= 100:
            raise InvalidArgs(f"priority 取值 0-100: {priority}")

    if idea_dao.get_idea(mem_conn, idea_id) is None:
        return _not_found(idea_id)
    ok = idea_dao.update_idea_content(
        mem_conn, idea_id, content=content, priority=priority,
    )
    if not ok:
        return OperationResult.fail(ERR_INTERNAL, "编辑失败")
    updated_fields = [f for f, v in (("content", content), ("priority", priority)) if v is not None]
    return OperationResult.succeed({
        "idea": idea_dao.get_idea(mem_conn, idea_id),
        "updated_fields": updated_fields,
    })


# ---------- 操作 4：追加式备注（可备注） ----------

def append_note(
    mem_conn: sqlite3.Connection,
    idea_id: str,
    *,
    text: str | None = None,
) -> OperationResult:
    """**追加**一条带时间戳的人工备注（绝不覆盖既有备注，设计 §4）。

    Args:
        mem_conn: memory.db 连接。
        idea_id: 创意 id。
        text: 备注正文，非空。

    Returns:
        成功时 data 为 ``{"idea_id", "notes", "count"}``——
        ``notes`` 是**完整**备注数组（含历史），``count`` 为其长度。

    Raises:
        InvalidArgs: text 缺失 / 空白 / 超长。
    """
    if text is None or not isinstance(text, str) or not text.strip():
        raise InvalidArgs("text 不能为空")
    if len(text) > MAX_NOTE_LEN:
        raise InvalidArgs(f"text 超长（上限 {MAX_NOTE_LEN} 字符）")

    notes = idea_dao.append_idea_note(mem_conn, idea_id, text)
    if notes is None:
        return _not_found(idea_id)
    return OperationResult.succeed({
        "idea_id": idea_id,
        "notes": notes,
        "count": len(notes),
    })


# ---------- 操作 5：设置人工标记（可标记） ----------

def set_flag(
    mem_conn: sqlite3.Connection,
    idea_id: str,
    *,
    custom_flag: str | None = None,
) -> OperationResult:
    """设置人工标记（自由文本，**无枚举校验**——升格/暂缓/自定义由用户说了算）。

    Args:
        mem_conn: memory.db 连接。
        idea_id: 创意 id。
        custom_flag: 标记文本；None 或空串表示清除标记。

    Returns:
        成功时 data 为 ``{"idea_id", "custom_flag"}``。

    Raises:
        InvalidArgs: 标记文本超长。
    """
    if custom_flag is not None:
        if not isinstance(custom_flag, str):
            raise InvalidArgs("custom_flag 必须是字符串")
        if len(custom_flag) > MAX_FLAG_LEN:
            raise InvalidArgs(f"custom_flag 超长（上限 {MAX_FLAG_LEN} 字符）")
        # 空白串归一为「清除标记」，避免库里出现看不见的空标记
        if not custom_flag.strip():
            custom_flag = None

    if idea_dao.get_idea(mem_conn, idea_id) is None:
        return _not_found(idea_id)
    ok = idea_dao.set_idea_flag(mem_conn, idea_id, custom_flag)
    if not ok:
        return OperationResult.fail(ERR_INTERNAL, "标记失败")
    return OperationResult.succeed({"idea_id": idea_id, "custom_flag": custom_flag})


# ---------- 操作 5.5：升格为需求（ST-7 创意池闭环，2026-08-12） ----------

def promote_idea(
    mem_conn: sqlite3.Connection,
    idea_id: str,
    *,
    title: str | None = None,
    content: str | None = None,
    priority: int | None = None,
    project_id: str | None = None,
    source_ref: str | None = None,
) -> OperationResult:
    """把创意升格为需求（联动：置 promoted 标记 + 创建 demand 回填 origin_idea_id）。

    创意池「升格」闭环（设计 SGME-WebUI设计-v0.1 §5）：
    1. 校验创意存在（不存在 → 404）
    2. 置 ``custom_flag='promoted'``（创意侧标记）
    3. 复用 ``demand.create_demand`` 创建需求，回填 ``origin_idea_id=idea_id`` 闭合溯源链

    Args:
        mem_conn: memory.db 连接。
        idea_id: 创意 id。
        title: 需求标题，**必填非空**。
        content: 需求描述（缺省用创意内容）。
        priority: 需求优先级 0-100（缺省 50）。
        project_id: 可选，关联项目。
        source_ref: 可选，需求溯源引用。

    Returns:
        成功 data 为 ``{"idea_id", "promoted": True, "demand": {...}}``。

    Raises:
        InvalidArgs: title 缺失/空白。
    """
    if idea_dao.get_idea(mem_conn, idea_id) is None:
        return _not_found(idea_id)
    if title is None or not isinstance(title, str) or not title.strip():
        raise InvalidArgs("title 不能为空（升格需求必须要有标题）")
    if len(title) > 200:
        raise InvalidArgs(f"title 超长（上限 200 字符）")

    # 置 promoted 标记（先标记，失败即中止，不产生半程需求）
    ok = idea_dao.set_idea_flag(mem_conn, idea_id, "promoted")
    if not ok:
        return OperationResult.fail(ERR_INTERNAL, "升格标记失败")

    # 复用需求池创建逻辑（函数内导入，避免模块顶部循环依赖）
    from sgme.operations.demand import create_demand

    body: dict[str, Any] = {
        "title": title,
        "origin_idea_id": idea_id,
    }
    if content is not None:
        body["content"] = content
    if priority is not None:
        body["priority"] = priority
    if project_id is not None:
        body["project_id"] = project_id
    if source_ref is not None:
        body["source_ref"] = source_ref

    res = create_demand(mem_conn, body)
    if not res.ok:
        return res  # demand 错误透传（400/404/500）

    return OperationResult.succeed({
        "idea_id": idea_id,
        "promoted": True,
        "demand": res.data,
    })


# ---------- 操作 5.5：人工添加创意（2026-08-13 用户定：创意由用户主动提出） ----------

def add_idea(
    mem_conn: sqlite3.Connection,
    *,
    content: str | None = None,
    priority: int | None = None,
    source_ref: str | None = None,
) -> OperationResult:
    """人工新增一条创意（用户主动提出才记录，不再依赖提炼 LLM 打标）。

    - 校验 content 非空（上限 MAX_CONTENT_LEN）；priority 0-100（缺省 50）
    - data 层自动打 ``ideas`` 标签 + ``ttl_days=NULL``（创意长期保存铁律）
    - 溯源 ``source_ref`` 可选，落 ``source_type='manual'``

    Returns:
        成功 data 为 ``{"idea": {...}, "created": True}``。
    """
    if content is None or not isinstance(content, str) or not content.strip():
        raise InvalidArgs("content 不能为空")
    if len(content) > MAX_CONTENT_LEN:
        raise InvalidArgs(f"content 超长（上限 {MAX_CONTENT_LEN} 字符）")
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            raise InvalidArgs(f"priority 必须是整数: {priority}") from None
        if not 0 <= priority <= 100:
            raise InvalidArgs(f"priority 取值 0-100: {priority}")
    if source_ref is not None and not isinstance(source_ref, str):
        raise InvalidArgs("source_ref 必须是字符串")

    memory_id = idea_dao.add_idea(
        mem_conn,
        content.strip(),
        priority=50 if priority is None else priority,
        source_ref=source_ref,
    )
    return OperationResult.succeed({
        "idea": idea_dao.get_idea(mem_conn, memory_id),
        "created": True,
    })


# ---------- 操作 6：软删除（可删除，可恢复） ----------

def soft_delete_idea(
    mem_conn: sqlite3.Connection,
    idea_id: str,
    *,
    reason: str | None = None,
) -> OperationResult:
    """软删除创意：`status='rejected'`，**绝不物理删除**（设计 §4「可恢复」）。

    Args:
        mem_conn: memory.db 连接。
        idea_id: 创意 id。
        reason: 删除说明；缺省 `idea_dao.DEFAULT_DISCARD_REASON`。

    Returns:
        成功时 data 为 ``{"idea_id", "status", "reject_reason", "deleted"}``；
        ``deleted`` 恒为 ``False``——显式声明「记录仍在库中」。
    """
    if idea_dao.get_idea(mem_conn, idea_id) is None:
        return _not_found(idea_id)
    final_reason = reason or idea_dao.DEFAULT_DISCARD_REASON
    ok = idea_dao.soft_delete_idea(mem_conn, idea_id, reason=final_reason)
    if not ok:
        return OperationResult.fail(ERR_INTERNAL, "软删除失败")
    return OperationResult.succeed({
        "idea_id": idea_id,
        "status": "rejected",
        "reject_reason": final_reason,
        "deleted": False,
    })


# ---------- 操作 7：恢复软删除 ----------

def restore_idea(mem_conn: sqlite3.Connection, idea_id: str) -> OperationResult:
    """撤销软删除：恢复 `status='active'`（设计 §4「可恢复」的对称动作）。

    Args:
        mem_conn: memory.db 连接。
        idea_id: 创意 id。

    Returns:
        成功时 data 为 ``{"idea_id", "status"}``。
    """
    if idea_dao.get_idea(mem_conn, idea_id) is None:
        return _not_found(idea_id)
    ok = idea_dao.restore_idea(mem_conn, idea_id)
    if not ok:
        return OperationResult.fail(ERR_INTERNAL, "恢复失败")
    return OperationResult.succeed({"idea_id": idea_id, "status": "active"})
