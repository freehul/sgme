"""operations/browse.py：F1 浏览类只读操作（0.8 T-15，契约 §5.3 / §5.5 / §5.6 / §5.7）。

承接的入口
----------
========================================  ==========================================
入口                                       操作
========================================  ==========================================
HTTP ``GET /v1/admin/memories``            ``list_memories``（契约 §5.3）
HTTP ``GET /v1/admin/refine_runs``         ``list_refine_runs``（契约 §5.5）
HTTP ``GET /v1/admin/sessions``            ``list_sessions``（契约 §5.6.1）
HTTP ``GET /v1/admin/sessions/{file_id}``  ``get_session_raw``（契约 §5.6.2 / §4.7 同构）
HTTP ``GET /v1/admin/stats/detail``        ``stats_detail``（契约 §5.7）
========================================  ==========================================

场景列表（契约 §5.4）见 ``operations/scene.py``——本模块导出的参数解析工具
（``parse_page`` / ``parse_limit`` / ``parse_status_list`` / ``page_envelope`` …）
由其平级复用，形如 ``operations/stats.py`` 复用 ``operations/health.watermark_age_sec``。

⚠️ 落点说明（T-15 实施偏差，已报备主控）
------------------------------------------
契约 §5.3.5 / §5.5.3 把 ``list_memories`` / refine 列表分别指向
``operations/memory.py`` 与 ``operations/refine.py``（文中标注"新建"）。但基线
7bac65b 上这两个文件**已存在**且不在本次授权修改清单内，故两个操作暂落本模块。
功能与契约完全一致，仅文件落点不同；后续如授权可原样平移，调用方只需改 import。

分层铁律（照抄 health.py 样板）
--------------------------------
1. 本模块**不认识协议**：不 import fastapi，不知道 HTTP 状态码。
   参数一律以「原始查询串」形态收入（``str | None``），由本层解析 + 校验，
   非法值抛 ``InvalidArgs``，入口层 ``run_operation`` 统一翻译为 400。
2. 一切 SQL 落 ``sgme.data.*`` DAO——尤其 ``stats_detail`` 的聚合查询严格走
   ``stats_dao.refine_detail``（统计 SQL 唯一出口，B30 铁律），本层零 SQL。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

from sgme.data import memory_dao, refine_dao, session_dao, stats_dao
from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs, OperationResult
from sgme.raw import store as raw_store

# ---------- 契约枚举与缺省值（§5.3.2 / §5.4.1 / §5.5.1 / §5.6.1 / §5.7） ----------

DEFAULT_PAGE: int = 1
DEFAULT_LIMIT: int = 50
#: 页大小硬上限。契约 §5.3.2「上限硬限制 200（防查询放大）」——超限**报错**而非静默截断，
#: 否则调用方会误以为拿到了完整页，分页游标随之错位。
MAX_LIMIT: int = 200

#: memories / scenes 共用的状态枚举（契约 §5.3.2 与 §5.4.1 取值一致）。
STATUS_VALUES: tuple[str, ...] = ("active", "rejected", "expired", "archived")
#: 「默认仅 active」——显式传 status 才能看见 rejected/expired/archived。
DEFAULT_STATUSES: tuple[str, ...] = ("active",)

ORDER_VALUES: tuple[str, ...] = ("desc", "asc")
DEFAULT_ORDER: str = "desc"

MEMORY_SORT_VALUES: tuple[str, ...] = ("updated_at", "occurred_at", "priority")
DEFAULT_MEMORY_SORT: str = "updated_at"

REFINE_STAGE_VALUES: tuple[str, ...] = (
    "l1_extraction", "l1_conflict", "l2_scene", "tier0_summary",
)
REFINE_STATUS_VALUES: tuple[str, ...] = ("running", "ok", "error")

SESSION_STATUS_VALUES: tuple[str, ...] = ("new", "refined", "archived")

PERIOD_VALUES: tuple[str, ...] = ("daily", "weekly", "monthly")
DEFAULT_PERIOD: str = "weekly"

#: 布尔查询参数的可接受写法（大小写不敏感）。
_TRUE_LITERALS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS: frozenset[str] = frozenset({"0", "false", "no", "off"})


def _now_iso() -> str:
    """当前 UTC ISO 时间戳（与全库 ``%Y-%m-%dT%H:%M:%SZ`` 口径一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 参数解析（浏览类端点共用；非法值一律 InvalidArgs → 400） ----------

def parse_page(raw: str | int | None) -> int:
    """解析 ``page``：整数且 ≥ 1，缺省 1。

    Args:
        raw: 原始查询串；None / 空串取缺省值。

    Returns:
        页码（≥ 1）。

    Raises:
        InvalidArgs: 非整数或 < 1。
    """
    if raw is None or raw == "":
        return DEFAULT_PAGE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidArgs(f"page 必须是整数: {raw}") from None
    if value < 1:
        raise InvalidArgs(f"page 必须 ≥ 1: {raw}")
    return value


def parse_limit(raw: str | int | None) -> int:
    """解析 ``limit``：整数且 1 ≤ limit ≤ 200，缺省 50。

    Args:
        raw: 原始查询串；None / 空串取缺省值。

    Returns:
        页大小（1..200）。

    Raises:
        InvalidArgs: 非整数、< 1 或 > ``MAX_LIMIT``（硬上限，不静默截断）。
    """
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidArgs(f"limit 必须是整数: {raw}") from None
    if value < 1:
        raise InvalidArgs(f"limit 必须 ≥ 1: {raw}")
    if value > MAX_LIMIT:
        raise InvalidArgs(f"limit 超过硬上限 {MAX_LIMIT}: {raw}")
    return value


def parse_choice(
    raw: str | None,
    allowed: Sequence[str],
    field: str,
    default: str | None = None,
) -> str | None:
    """解析单值枚举参数。

    Args:
        raw: 原始查询串；None / 空串取 ``default``。
        allowed: 允许的取值。
        field: 参数名（用于错误文案）。
        default: 缺省值，None 表示「不过滤」。

    Returns:
        枚举值或 ``default``。

    Raises:
        InvalidArgs: 取值不在 ``allowed`` 内。
    """
    if raw is None or raw == "":
        return default
    value = str(raw).strip().lower()
    if value not in allowed:
        raise InvalidArgs(f"{field} 取值非法: {raw}（可选 {'/'.join(allowed)}）")
    return value


def parse_status_list(
    raw: str | None,
    allowed: Sequence[str] = STATUS_VALUES,
    field: str = "status",
    default: Sequence[str] = DEFAULT_STATUSES,
) -> tuple[str, ...]:
    """解析逗号分隔的多值状态参数（契约 §5.3.2）。

    「默认仅 active」是产品硬约定：浏览端点在不传 status 时**不得**返回
    rejected / expired / archived，避免用户已判错的记忆重新出现在界面上。

    Args:
        raw: 形如 ``"active,rejected"``；None / 空串取 ``default``。
        allowed: 允许的状态枚举。
        field: 参数名（用于错误文案）。
        default: 缺省状态元组。

    Returns:
        去重且保序的状态元组。

    Raises:
        InvalidArgs: 含枚举外取值，或去重后为空（如传入 ``","``）。
    """
    if raw is None or raw == "":
        return tuple(default)
    out: list[str] = []
    for chunk in str(raw).split(","):
        value = chunk.strip().lower()
        if not value:
            continue
        if value not in allowed:
            raise InvalidArgs(f"{field} 取值非法: {chunk}（可选 {'/'.join(allowed)}）")
        if value not in out:
            out.append(value)
    if not out:
        raise InvalidArgs(f"{field} 不能为空")
    return tuple(out)


def parse_bool(raw: str | bool | None, field: str, default: bool = False) -> bool:
    """解析布尔查询参数（``true/false/1/0/yes/no/on/off``，大小写不敏感）。

    Raises:
        InvalidArgs: 无法识别的字面量。
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in _TRUE_LITERALS:
        return True
    if value in _FALSE_LITERALS:
        return False
    raise InvalidArgs(f"{field} 必须是布尔值: {raw}")


def parse_timestamp(raw: str | None, field: str) -> str | None:
    """校验 ISO8601 时间戳并**原样返回**（不做格式归一）。

    库内时间戳统一为 ``%Y-%m-%dT%H:%M:%SZ``，与查询串按字典序比较即等价于按
    时间比较，故此处只校验可解析性、不改写取值——改写反而会引入时区歧义。
    允许裸日期（``2026-08-09``）：作为当日 00:00 的简写，字典序语义天然正确。

    Args:
        raw: 原始查询串；None / 空串返回 None（不过滤）。
        field: 参数名（用于错误文案）。

    Returns:
        原始字符串或 None。

    Raises:
        InvalidArgs: 无法按 ISO8601 解析。
    """
    if raw is None or raw == "":
        return None
    value = str(raw).strip()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise InvalidArgs(f"{field} 必须是 ISO8601 时间戳: {raw}") from None
    return value


def page_envelope(
    items: list[dict[str, Any]],
    total: int,
    page: int,
    limit: int,
) -> dict[str, Any]:
    """分页响应外层信封（契约 §5.3.3 / §5.4.2 / §5.5.2 / §5.6.1 键序一致）。

    Args:
        items: 当前页条目。
        total: 过滤条件下的总条数（非当前页条数）。
        page: 页码。
        limit: 页大小。

    Returns:
        ``{"items", "count", "total", "page", "limit", "generated_at"}``。
    """
    return {
        "items": items,
        "count": len(items),
        "total": int(total),
        "page": page,
        "limit": limit,
        "generated_at": _now_iso(),
    }


# ---------- 操作 1：GET /v1/admin/memories（契约 §5.3） ----------

def list_memories(
    mem_conn: sqlite3.Connection,
    *,
    page: str | int | None = None,
    limit: str | int | None = None,
    dimension_id: str | None = None,
    dimensions: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    since: str | None = None,
    until: str | None = None,
    ttl_filter: str | bool | None = None,
) -> OperationResult:
    """记忆分页列表（WebUI 记忆浏览 / SCSM 记忆面板数据源）。

    语义要点：
    - ``status`` 缺省仅 ``active``——rejected / expired / archived 需显式索取；
    - ``since`` / ``until`` 作用于 ``sort`` 指定的字段（非固定 updated_at）；
    - ``ttl_filter`` 缺省 false：浏览语义是「看全部」，TTL 过滤属可选增强；
    - ``notes`` / ``custom_flag`` 为 0.8 ST-14 新增列，本分支基线尚无——
      DAO 侧按 ``PRAGMA table_info`` 探测，列不存在时返回 null（合并 ST-14 后自动生效）。
    - 维度过滤（2026-08-13）：``dimensions`` 逗号分隔多维度（**AND 语义**——
      每个勾选维度都必须命中），与单 ``dimension_id`` 二选一；两者都传时
      ``dimensions`` 优先。

    Args:
        mem_conn: memory.db 连接。
        page / limit / dimension_id / dimensions / status / sort / order / since /
            until / ttl_filter: 契约 §5.3.2 查询参数的原始串形态。

    Returns:
        ``OperationResult(ok=True)``，data 为契约 §5.3.3 响应体
        （``items`` 条目含 memory_id / content / dimensions / memory_type /
        priority / status / created_at / updated_at / occurred_at / notes /
        custom_flag / source_ref）。

    Raises:
        InvalidArgs: 任一查询参数非法（入口层翻译为 400 ERR_INVALID_ARGS）。
    """
    page_num = parse_page(page)
    limit_num = parse_limit(limit)
    statuses = parse_status_list(status)
    sort_field = parse_choice(sort, MEMORY_SORT_VALUES, "sort", DEFAULT_MEMORY_SORT)
    order_dir = parse_choice(order, ORDER_VALUES, "order", DEFAULT_ORDER)
    since_ts = parse_timestamp(since, "since")
    until_ts = parse_timestamp(until, "until")
    ttl_on = parse_bool(ttl_filter, "ttl_filter", default=False)
    # 维度：dimensions 逗号分隔列表优先，否则单 dimension_id 兜底
    dim_ids: list[str] | None = None
    if dimensions:
        dim_ids = [d.strip() for d in dimensions.split(",") if d.strip()]
    elif dimension_id:
        dim_ids = [dimension_id]

    items, total = memory_dao.list_memories_page(
        mem_conn,
        page=page_num,
        limit=limit_num,
        dimension_ids=dim_ids,
        statuses=statuses,
        sort=sort_field or DEFAULT_MEMORY_SORT,
        order=order_dir or DEFAULT_ORDER,
        since=since_ts,
        until=until_ts,
        ttl_filter=ttl_on,
    )
    return OperationResult.succeed(page_envelope(items, total, page_num, limit_num))


# ---------- 操作 2：GET /v1/admin/refine_runs（契约 §5.5） ----------

def list_refine_runs(
    mem_conn: sqlite3.Connection,
    *,
    page: str | int | None = None,
    limit: str | int | None = None,
    stage: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> OperationResult:
    """提炼记录分页（WebUI 提炼监控 / SCSM 健康观测数据源）。

    与 memories/scenes 不同，本端点**无 status 缺省过滤**——提炼监控恰恰要看
    error 与 running，默认隐藏会让异常不可见。排序固定 ``started_at DESC``
    （监控场景只关心最近批次，契约 §5.5.1 未开放 sort 参数）。

    Args:
        mem_conn: memory.db 连接（``refine_runs`` 表在 memory.db）。
        page / limit / stage / status / since / until: 契约 §5.5.1 查询参数。

    Returns:
        ``OperationResult(ok=True)``，data 为契约 §5.5.2 响应体。
        ``action_counts`` 按库内原始 JSON **字符串**透传（契约示例即字符串形态）。

    Raises:
        InvalidArgs: 任一查询参数非法。
    """
    page_num = parse_page(page)
    limit_num = parse_limit(limit)
    stage_value = parse_choice(stage, REFINE_STAGE_VALUES, "stage", None)
    status_value = parse_choice(status, REFINE_STATUS_VALUES, "status", None)
    since_ts = parse_timestamp(since, "since")
    until_ts = parse_timestamp(until, "until")

    items, total = refine_dao.list_runs_page(
        mem_conn,
        page=page_num,
        limit=limit_num,
        stage=stage_value,
        status=status_value,
        since=since_ts,
        until=until_ts,
    )
    return OperationResult.succeed(page_envelope(items, total, page_num, limit_num))


# ---------- 操作 3：GET /v1/admin/sessions（契约 §5.6.1） ----------

def list_sessions(
    session_conn: sqlite3.Connection,
    *,
    page: str | int | None = None,
    limit: str | int | None = None,
    session_key: str | None = None,
    agent_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> OperationResult:
    """L0 会话列表（raw_files 索引分页）。

    ``session_key`` 为**子串匹配**（契约 §5.6.1，如 ``hermes-`` 前缀过滤），
    ``agent_id`` 为精确匹配；时间范围作用于 ``started_at``。

    Args:
        session_conn: session.db 连接（v0.7 拆分后 raw_files 在 session.db，勿查 wiki.db）。
        page / limit / session_key / agent_id / status / since / until: 契约 §5.6.1 查询参数。

    Returns:
        ``OperationResult(ok=True)``，data 为契约 §5.6.1 响应体。

    Raises:
        InvalidArgs: 任一查询参数非法。
    """
    page_num = parse_page(page)
    limit_num = parse_limit(limit)
    status_value = parse_choice(status, SESSION_STATUS_VALUES, "status", None)
    since_ts = parse_timestamp(since, "since")
    until_ts = parse_timestamp(until, "until")

    items, total = session_dao.list_raw_files_page(
        session_conn,
        page=page_num,
        limit=limit_num,
        session_key=(session_key or None),
        agent_id=(agent_id or None),
        status=status_value,
        since=since_ts,
        until=until_ts,
    )
    return OperationResult.succeed(page_envelope(items, total, page_num, limit_num))


# ---------- 操作 4：GET /v1/admin/sessions/{file_id}（契约 §5.6.2 / §4.7 同构） ----------

def get_session_raw(
    session_conn: sqlite3.Connection,
    file_id: str,
) -> OperationResult:
    """读取单条 L0 会话原文（UI 溯源）。

    正文取自 ``raw/sessions/{file_id}.md``——路径经 ``raw.store.file_path``
    解析（其内部读 ``config.RAW_DIR``，测试注入的隔离 raw 目录因此自动生效）。

    **不做鉴权归属校验**：单用户语义，Admin Key 可读任何 file_id
    （与契约 §4.7 一致，多租户留待 v2）。

    Args:
        session_conn: session.db 连接。
        file_id: raw_files 主键。

    Returns:
        - 成功：data 为 ``{"file_id", "session_key", "agent_id", "content"}``（§4.7 键序）。
        - 索引无此 file_id 或磁盘原文缺失：``ok=False`` + ERR_NOT_FOUND（入口层 → 404）。
    """
    if not file_id:
        return OperationResult.fail(ERR_NOT_FOUND, "会话不存在: (空 file_id)")

    row = session_dao.get_raw_file(session_conn, file_id)
    if not row:
        return OperationResult.fail(ERR_NOT_FOUND, f"会话不存在: {file_id}")

    path = raw_store.file_path(file_id)
    if not path.exists():
        # 索引在、文件不在：对调用方而言原文同样「取不到」，语义仍是 404。
        # 不降级为空串——那会让 UI 把「文件丢失」误显示为「空会话」。
        return OperationResult.fail(ERR_NOT_FOUND, f"会话原文不存在: {file_id}")

    content = path.read_text(encoding="utf-8")
    return OperationResult.succeed({
        "file_id": row["file_id"],
        "session_key": row["session_key"],
        "agent_id": row["agent_id"],
        "content": content,
    })


# ---------- 操作 5：GET /v1/admin/stats/detail（契约 §5.7） ----------

def stats_detail(
    mem_conn: sqlite3.Connection,
    *,
    period: str | None = None,
    stage: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> OperationResult:
    """token 成本 / 质量明细（WebUI 提炼监控图表数据源）。

    聚合 SQL 全部在 ``stats_dao.refine_detail``——统计查询唯一出口（B30 铁律），
    本层只做参数校验与响应组装，**不拼一行聚合 SQL**。

    Args:
        mem_conn: memory.db 连接（``refine_runs`` 在 memory.db）。
        period: ``daily`` / ``weekly``（缺省）/ ``monthly``。
        stage: 可选 stage 过滤；不传时按 stage 分行。
        from_ts / to_ts: 时间范围，作用于 ``started_at``（契约参数名 ``from`` / ``to``，
            ``from`` 是 Python 保留字，故入口层以别名映射到本形参）。

    Returns:
        ``OperationResult(ok=True)``，data 为 ``{"items", "totals", "generated_at"}``
        （契约 §5.7）。本端点无分页参数，故不套 ``page_envelope``。

    Raises:
        InvalidArgs: 任一查询参数非法。
    """
    period_value = parse_choice(period, PERIOD_VALUES, "period", DEFAULT_PERIOD)
    stage_value = parse_choice(stage, REFINE_STAGE_VALUES, "stage", None)
    from_value = parse_timestamp(from_ts, "from")
    to_value = parse_timestamp(to_ts, "to")

    items, totals = stats_dao.refine_detail(
        mem_conn,
        period=period_value or DEFAULT_PERIOD,
        stage=stage_value,
        from_ts=from_value,
        to_ts=to_value,
    )
    return OperationResult.succeed({
        "items": items,
        "totals": totals,
        "generated_at": _now_iso(),
    })
