"""operations/scene.py：场景浏览操作（0.8 T-15，契约 §5.4）。

承接的入口
----------
========================================  ==========================================
入口                                       操作
========================================  ==========================================
HTTP ``GET /v1/admin/scenes``              ``list_scenes``（契约 §5.4）
========================================  ==========================================

与 ``operations/browse.py`` 的关系：本模块复用其参数解析工具与分页信封，
形如 ``operations/stats.py`` 平级复用 ``operations/health.watermark_age_sec``——
同层 import，无环。场景独立成模块是契约 §5.4.3 的明确要求
（后续 L2 场景的写操作也归本模块，届时不必再搬家）。

分层铁律：本模块不认识协议，SQL 全部落 ``sgme.data.scene_dao``。
"""
from __future__ import annotations

import sqlite3

from sgme.data import scene_dao
from sgme.operations.browse import (
    DEFAULT_ORDER,
    ORDER_VALUES,
    page_envelope,
    parse_choice,
    parse_limit,
    parse_page,
    parse_status_list,
    parse_timestamp,
)
from sgme.operations.errors import OperationResult

#: 契约 §5.4.1：场景排序字段，缺省 ``heat``（热度优先——场景列表的产品语义是
#: 「哪些叙事最活跃」，而非「哪些最近被碰过」，故不与 memories 的 updated_at 对齐）。
SCENE_SORT_VALUES: tuple[str, ...] = ("heat", "updated_at", "created_at")
DEFAULT_SCENE_SORT: str = "heat"


def list_scenes(
    mem_conn: sqlite3.Connection,
    *,
    page: str | int | None = None,
    limit: str | int | None = None,
    status: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> OperationResult:
    """L2 场景分页列表（WebUI 场景浏览数据源）。

    语义要点：
    - ``status`` 缺省仅 ``active``（与 memories 同口径：rejected / expired 需显式索取）；
    - ``sort`` 缺省 ``heat``，``since`` / ``until`` 作用于 ``sort`` 指定的字段——
      当 sort=heat 时时间过滤对 heat 列做字典序比较无意义，故此时**时间范围作用于
      ``updated_at``**（见 ``scene_dao.list_scenes_page`` 的 time_column 约定），
      避免用户传 since 却拿到空列表；
    - ``memories_count`` 来自 ``scene_memories`` 关联计数（子查询聚合，非 JOIN，
      避免与分页 LIMIT 相互干扰）。

    Args:
        mem_conn: memory.db 连接（v0.7 三库拆分后 scenes 系列在 memory.db）。
        page / limit / status / sort / order / since / until: 契约 §5.4.1 查询参数。

    Returns:
        ``OperationResult(ok=True)``，data 为契约 §5.4.2 响应体（``items`` 条目含
        scene_id / title / content / heat / status / memories_count /
        created_at / updated_at）。

    Raises:
        InvalidArgs: 任一查询参数非法（入口层翻译为 400 ERR_INVALID_ARGS）。
    """
    page_num = parse_page(page)
    limit_num = parse_limit(limit)
    statuses = parse_status_list(status)
    sort_field = parse_choice(sort, SCENE_SORT_VALUES, "sort", DEFAULT_SCENE_SORT)
    order_dir = parse_choice(order, ORDER_VALUES, "order", DEFAULT_ORDER)
    since_ts = parse_timestamp(since, "since")
    until_ts = parse_timestamp(until, "until")

    items, total = scene_dao.list_scenes_page(
        mem_conn,
        page=page_num,
        limit=limit_num,
        statuses=statuses,
        sort=sort_field or DEFAULT_SCENE_SORT,
        order=order_dir or DEFAULT_ORDER,
        since=since_ts,
        until=until_ts,
    )
    return OperationResult.succeed(page_envelope(items, total, page_num, limit_num))
