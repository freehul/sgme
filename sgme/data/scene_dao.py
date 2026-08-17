"""storage/scene_dao.py：memory.db 的 L2 场景 DAO（scenes / scene_memories / scene_versions）。

v0.7 三库拆分：本模块的 11 个函数由 `storage/wiki_dao.py` 原样迁入，
**函数名、签名、函数体一行不改**，只改所在文件与传入连接（wiki_conn → mem_conn）。

分层职责：本模块写 `content_seg`（jieba 分词，对称 memory_dao）；
`scenes_fts` 虚拟表 / 触发器 / rebuild 归 search 层 `init_scenes_fts()`，本模块不碰。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from sgme.segment import segment


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- scenes CRUD（v0.4 T9：L2 场景叙事文档） ----------

def insert_scene(
    conn: sqlite3.Connection,
    scene_id: str,
    title: str,
    content: str,
    created_at: str | None = None,
    updated_at: str | None = None,
    last_memory_added_at: str | None = None,
) -> str:
    """新建场景：heat=1, status='active'。

    created_at/updated_at 缺省取当前 UTC ISO 时间戳。
    """
    c_at = created_at or _now_iso()
    u_at = updated_at or c_at
    # v5：data 层写 content_seg（jieba 分词），FTS 触发器只同步不分词（对称 memory_dao）
    seg_text = segment(f"{title} {content}")
    conn.execute(
        """
        INSERT INTO scenes
          (scene_id, title, content, heat, status, created_at, updated_at,
           last_memory_added_at, content_seg)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (scene_id, title, content, 1, "active", c_at, u_at, last_memory_added_at, seg_text),
    )
    conn.commit()
    return scene_id


def get_scene(conn: sqlite3.Connection, scene_id: str) -> dict | None:
    """返回单条场景或 None。"""
    cur = conn.execute("SELECT * FROM scenes WHERE scene_id=?", (scene_id,))
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def update_scene_content(
    conn: sqlite3.Connection,
    scene_id: str,
    content: str,
    updated_at: str | None = None,
    heat_increment: int = 1,
    last_memory_added_at: str | None = None,
) -> bool:
    """更新场景内容 + heat 自增（默认 +1，合并场景时调用方可传 sum+1）。

    updated_at 缺省取当前 UTC ISO 时间戳。
    last_memory_added_at 非空时同步更新（记录最近一次关联记忆的时间）。
    """
    u_at = updated_at or _now_iso()
    # v5：content 变更时同事务刷新 content_seg（title 不变，读当前值拼接）
    row = conn.execute("SELECT title FROM scenes WHERE scene_id=?", (scene_id,)).fetchone()
    seg_text = segment(f"{row[0]} {content}") if row else segment(content)
    sets = ["content=?", "content_seg=?", "heat=heat+?", "updated_at=?"]
    params: list = [content, seg_text, heat_increment, u_at]
    if last_memory_added_at is not None:
        sets.append("last_memory_added_at=?")
        params.append(last_memory_added_at)
    params.append(scene_id)
    cur = conn.execute(
        f"UPDATE scenes SET {', '.join(sets)} WHERE scene_id=?",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


def update_scene_status(conn: sqlite3.Connection, scene_id: str, status: str) -> bool:
    """更新场景状态（软删除/恢复：'active' / 'archived'）。"""
    cur = conn.execute(
        "UPDATE scenes SET status=? WHERE scene_id=?",
        (status, scene_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_active_scenes(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """列出 active 场景（updated_at DESC）。"""
    sql = "SELECT * FROM scenes WHERE status='active' ORDER BY updated_at DESC"
    params: list = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def list_scenes_over_threshold(conn: sqlite3.Connection, threshold: int) -> int:
    """返回当前 active 场景总数（调用方与 threshold 比较判断红/橙/黄预警级别）。

    threshold 参数为预警阈值，函数本身不参与过滤——
    返回 active 场景数后由调用方对比 threshold 决定是否触发 anomaly_warn。
    """
    cur = conn.execute("SELECT COUNT(*) AS c FROM scenes WHERE status='active'")
    return cur.fetchone()["c"]


def count_scenes(conn: sqlite3.Connection, status: str = "active") -> int:
    """按状态计数场景。"""
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM scenes WHERE status=?", (status,)
    )
    return cur.fetchone()["c"]


# ---------- scene_memories ----------

def add_memory_link(conn: sqlite3.Connection, scene_id: str, memory_id: str) -> None:
    """关联场景与记忆（INSERT OR IGNORE 幂等：重复添加不报错）。"""
    conn.execute(
        "INSERT OR IGNORE INTO scene_memories (scene_id, memory_id) VALUES (?,?)",
        (scene_id, memory_id),
    )
    conn.commit()


def list_memories_for_scene(conn: sqlite3.Connection, scene_id: str) -> list[str]:
    """返回场景关联的记忆 id 列表（按 memory_id ASC）。"""
    cur = conn.execute(
        "SELECT memory_id FROM scene_memories WHERE scene_id=? ORDER BY memory_id",
        (scene_id,),
    )
    return [r["memory_id"] for r in cur.fetchall()]


# ---------- scene_versions ----------

def insert_scene_version(
    conn: sqlite3.Connection,
    version_id: str,
    scene_id: str,
    content: str,
    version_at: str | None = None,
    reason: str | None = None,
) -> str:
    """归档场景历史版本（L2 UPDATE/MERGE 前置快照）。

    version_at 缺省取当前 UTC ISO 时间戳。
    reason 可选：归档原因（如 'update' / 'merge' / 'soft_delete'）。
    """
    v_at = version_at or _now_iso()
    conn.execute(
        """
        INSERT INTO scene_versions (version_id, scene_id, content, version_at, reason)
        VALUES (?,?,?,?,?)
        """,
        (version_id, scene_id, content, v_at, reason),
    )
    conn.commit()
    return version_id


def list_scene_versions(conn: sqlite3.Connection, scene_id: str) -> list[dict]:
    """返回场景历史版本（按 version_at ASC）。"""
    cur = conn.execute(
        "SELECT * FROM scene_versions WHERE scene_id=? ORDER BY version_at ASC",
        (scene_id,),
    )
    return [dict(r) for r in cur.fetchall()]

# ---------- 浏览分页（0.8 T-15 / 契约 §5.4） ----------

#: 排序字段白名单（无法参数化，必须映射后拼接）。
_BROWSE_SCENE_SORT_COLUMNS: dict[str, str] = {
    "heat": "s.heat",
    "updated_at": "s.updated_at",
    "created_at": "s.created_at",
}


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
    """场景分页查询（契约 §5.4；WebUI 场景浏览数据源）。

    与 ``list_active_scenes`` 的分工：那个是内部取全量 active（L2 合并链路用），
    本函数面向浏览——需要状态白名单、任意排序、时间窗与 total 计数。

    实现要点：
    - ``memories_count`` 走**标量子查询**而非 JOIN + GROUP BY：JOIN 会让
      LIMIT 作用在展开后的行上，分页条数失真；
    - 时间过滤列的选择：sort=heat 时 heat 是整数列，对其做 ISO 字符串比较无意义，
      故此时 since/until **改作用于 updated_at**（契约 §5.4.1 的时间窗语义是
      「筛一段时间内的场景」，不是「筛热度区间」）；sort 为时间列时即其自身。
    - ORDER BY 追加 ``scene_id`` 决胜键，保证分页稳定（heat 大量同值时尤其必要）。

    Args:
        conn: memory.db 连接（v0.7 三库拆分后 scenes 在 memory.db）。
        page: 页码（≥ 1，调用方已校验）。
        limit: 页大小（1..200，调用方已校验）。
        statuses: 状态白名单；空集合表示不过滤。
        sort: ``heat`` / ``updated_at`` / ``created_at``。
        order: ``desc`` / ``asc``。
        since / until: 时间窗闭区间边界（作用列见上）。

    Returns:
        ``(items, total)``。

    Raises:
        ValueError: ``sort`` 不在白名单内。
    """
    sort_col = _BROWSE_SCENE_SORT_COLUMNS.get(sort)
    if sort_col is None:
        raise ValueError(f"不支持的排序字段: {sort}")
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
        f"SELECT COUNT(*) AS c FROM scenes s{where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT s.scene_id, s.title, s.content, s.heat, s.status,
               s.created_at, s.updated_at,
               (SELECT COUNT(*) FROM scene_memories sm
                 WHERE sm.scene_id = s.scene_id) AS memories_count
        FROM scenes s{where_sql}
        ORDER BY {sort_col} {direction}, s.scene_id {direction}
        LIMIT ? OFFSET ?
        """,
        params + [int(limit), max(0, (int(page) - 1) * int(limit))],
    ).fetchall()

    items = [dict(r) for r in rows]
    # related_memories（2026-08-18 T-55 后续：WebUI 场景详情展示关联记忆）：
    # 每场景关联记忆前 N 条（content 截断 + 维度标签），批量查询避免 N+1
    if items:
        _RELATED_LIMIT = 5
        scene_ids = [it["scene_id"] for it in items]
        ph = ",".join("?" * len(scene_ids))
        rel_rows = conn.execute(
            f"""
            SELECT sm.scene_id, m.memory_id, m.content, m.updated_at
            FROM scene_memories sm
            JOIN memories m ON m.memory_id = sm.memory_id
            WHERE sm.scene_id IN ({ph})
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
