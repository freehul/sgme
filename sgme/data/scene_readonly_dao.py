"""data/scene_readonly_dao.py：已退役场景四表的**只读**分页 DAO（T-427）。

背景
----
场景层已正式退役（T-427，2026-11-20）：`data/scene_dao.py` 的写/聚合函数已摘除，
但 `GET /v1/admin/scenes` 必须保持只读可用（WebUI 场景页降级为冻结数据浏览）。
故本模块只保留浏览所需的最小查询：

- `list_scenes_page`：契约 §5.4 分页列表（status 白名单 / 排序 / 时间窗 / total）；
- 表名解析：优先 `scenes`，不存在时回落 `scenes_retired`（四表 rename 后端点仍 200）；
  子表 `scene_memories` 同理；缺表按 0 关联处理，不抛异常。

分层职责：data 层唯一 DB 访问；operations/scene.py 只做参数解析与信封组装。
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

#: 排序字段白名单（无法参数化，必须映射后拼接）。
_BROWSE_SCENE_SORT_COLUMNS: dict[str, str] = {
    "heat": "s.heat",
    "updated_at": "s.updated_at",
    "created_at": "s.created_at",
}


def _resolve_table(conn: sqlite3.Connection, base: str) -> str | None:
    """解析基础表名：先 base，再 base_retired；都不存在返回 None。"""
    for name in (base, f"{base}_retired"):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if row is not None:
            return name
    return None


def list_scenes_page(
    conn: sqlite3.Connection,
    *,
    page: int = 1,
    limit: int = 50,
    statuses: Iterable[str] = ("active",),
    sort: str = "heat",
    order: str = "desc",
    since: str | None = None,
    until: str | None = None,
) -> tuple[list[dict], int]:
    """场景分页查询（契约 §5.4；WebUI 场景浏览数据源，只读）。

    实现要点：
    - ``memories_count`` 走**标量子查询**而非 JOIN + GROUP BY：JOIN 会让
      LIMIT 作用在展开后的行上，分页条数失真；
    - 时间过滤列的选择：sort=heat 时 heat 是整数列，对其做 ISO 字符串比较无意义，
      故此时 since/until **改作用于 updated_at**；sort 为时间列时即其自身。
    - ORDER BY 追加 ``scene_id`` 决胜键，保证分页稳定；
    - 四表 rename 为 ``*_retired`` 后本函数自动回落，端点保持 200（冻结数据只读）。

    Args:
        conn: memory.db 连接。
        page: 页码（≥ 1，调用方已校验）。
        limit: 页大小（1..200，调用方已校验）。
        statuses: 状态白名单；空集合表示不过滤。
        sort: ``heat`` / ``updated_at`` / ``created_at``。
        order: ``desc`` / ``asc``。
        since / until: 时间窗闭区间边界（作用列见上）。

    Returns:
        ``(items, total)``；场景表不存在时返回 ``([], 0)``。

    Raises:
        ValueError: ``sort`` 不在白名单内。
    """
    sort_col = _BROWSE_SCENE_SORT_COLUMNS.get(sort)
    if sort_col is None:
        raise ValueError(f"不支持的排序字段: {sort}")

    scenes_table = _resolve_table(conn, "scenes")
    if scenes_table is None:
        return [], 0
    scene_memories_table = _resolve_table(conn, "scene_memories")

    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    time_col = sort_col if sort in ("updated_at", "created_at") else "s.updated_at"

    where: list[str] = []
    params: list = []
    status_list = [s for s in (statuses or ())]
    if status_list:
        where.append(f"s.status IN ({','.join('?' * len(status_list))})")
        params.extend(status_list)
    if since:
        where.append(f"{time_col} >= ?")
        params.append(since)
    if until:
        where.append(f"{time_col} <= ?")
        params.append(until)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM {scenes_table} s{where_sql}", params
    ).fetchone()["c"]

    memories_count_sql = (
        f"(SELECT COUNT(*) FROM {scene_memories_table} sm WHERE sm.scene_id = s.scene_id)"
        if scene_memories_table else "0"
    )
    rows = conn.execute(
        f"""
        SELECT s.scene_id, s.title, s.content, s.heat, s.status,
               s.created_at, s.updated_at,
               {memories_count_sql} AS memories_count
        FROM {scenes_table} s{where_sql}
        ORDER BY {sort_col} {direction}, s.scene_id {direction}
        LIMIT ? OFFSET ?
        """,
        params + [int(limit), max(0, (int(page) - 1) * int(limit))],
    ).fetchall()

    items = [dict(r) for r in rows]
    # related_memories（2026-08-18 T-55 后续：WebUI 场景详情展示关联记忆）：
    # 每场景关联记忆前 N 条（content 截断 + 维度标签），批量查询避免 N+1。
    # T-171（2026-09-18）：必须过滤 status='rejected'——已拒绝记忆的正文
    # （截断 120 字）不得经 GET /v1/admin/scenes 泄漏给调用端。
    if items and scene_memories_table:
        _RELATED_LIMIT = 5
        scene_ids = [it["scene_id"] for it in items]
        ph = ",".join("?" * len(scene_ids))
        rel_rows = conn.execute(
            f"""
            SELECT sm.scene_id, m.memory_id, m.content, m.updated_at
            FROM {scene_memories_table} sm
            JOIN memories m ON m.memory_id = sm.memory_id
            WHERE sm.scene_id IN ({ph})
              AND m.status != 'rejected'
            ORDER BY sm.scene_id, m.updated_at DESC
            """,
            scene_ids,
        ).fetchall()
        rel_map: dict[str, list[dict]] = {}
        mem_ids: list[str] = []
        for r in rel_rows:
            rel_map.setdefault(r["scene_id"], []).append({
                "memory_id": r["memory_id"],
                "content": (r["content"] or "")[:120],
                "updated_at": r["updated_at"],
            })
            mem_ids.append(r["memory_id"])
        dim_map: dict[str, list[str]] = {}
        if mem_ids:
            ph2 = ",".join("?" * len(mem_ids))
            for row in conn.execute(
                f"SELECT memory_id, dimension_id FROM memory_tags WHERE memory_id IN ({ph2})",
                mem_ids,
            ).fetchall():
                dim_map.setdefault(row["memory_id"], []).append(row["dimension_id"])
        for it in items:
            rel = rel_map.get(it["scene_id"], [])[:_RELATED_LIMIT]
            for m in rel:
                m["dimensions"] = dim_map.get(m["memory_id"], [])
            it["related_memories"] = rel

    return items, int(total)
