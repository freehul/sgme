"""data/demand_dao.py：memory.db 的需求池 DAO（demands 表，0.8 ST-15）。

职责：需求池的全部 SQL 出口（CRUD + 分页查询 + 计数 + 软校验探测）。
分层铁律 B30——**data 是唯一数据库操作层**：本模块之外（operations / server）
不得出现任何 demands 相关 SQL 字符串。

表结构与状态语义见 `docs/design/SGME-数据模型设计-v0.1.md` → **demands**：
状态流转 pending（未立项）→ planned（已立项）→ partial（部分解决）→ done（已解决）。

本模块**不做业务校验**（状态枚举、优先级范围、resolved_at 联动一律归 operations 层），
只做两件事：
1. 把参数安全地翻译成 SQL（排序字段/可写列均走白名单，杜绝拼接注入）；
2. 把结果行翻译成 dict。

⚠️ 表定义**不带外键约束**（project_id / origin_idea_id 均为裸 TEXT）。理由见
`_migrate_demands_table` 的 docstring；跨表存在性校验由本模块的
`project_meta_available` / `project_exists` 提供**软校验**原料，是否拦截由 operations 决定。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

# 可排序列白名单（防 ORDER BY 注入：sort 参数会被拼进 SQL）
SORT_FIELDS: tuple[str, ...] = ("priority", "updated_at", "created_at")

# 可排序方向白名单
ORDER_DIRECTIONS: tuple[str, ...] = ("asc", "desc")

# PATCH 可写列白名单（status 不在其中——状态流转走独立端点，带 resolved_at 联动）
EDITABLE_FIELDS: tuple[str, ...] = (
    "title",
    "content",
    "priority",
    "project_id",
    "source_ref",
)

# 时间型列（since/until 范围过滤只允许作用于这两列）
TIME_FIELDS: tuple[str, ...] = ("updated_at", "created_at")

# SELECT 出参列序（与数据模型文档字段顺序一致，避免 SELECT * 受建表顺序影响）
_COLUMNS: str = (
    "demand_id, title, content, status, priority, project_id, "
    "origin_idea_id, source_ref, created_at, updated_at, resolved_at"
)


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（与 db.py / scene_dao 同格式，定宽可字典序比较）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_demand_id() -> str:
    """生成需求主键（uuid4 字符串，对齐 memory_dao.new_id 惯例）。"""
    return str(uuid.uuid4())


def _row_to_dict(row: Any, cursor: sqlite3.Cursor | None = None) -> dict[str, Any]:
    """sqlite3.Row / 裸 tuple → dict（对齐 scene_dao.get_scene 的容错写法）。"""
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in cursor.description] if cursor is not None else []
    return dict(zip(cols, row))


def _like_pattern(text: str) -> str:
    r"""构造子串 LIKE 模式并转义通配符。

    用户输入的 `%` / `_` / `\` 必须转义，否则 `q=100%` 会退化成全表匹配。
    配套 SQL 使用 ``ESCAPE '\'``。
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ---------- 写入 ----------

def insert_demand(
    conn: sqlite3.Connection,
    demand_id: str,
    title: str,
    content: str = "",
    status: str = "pending",
    priority: int = 50,
    project_id: str | None = None,
    origin_idea_id: str | None = None,
    source_ref: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    resolved_at: str | None = None,
) -> str:
    """插入一条需求，返回 demand_id。

    created_at / updated_at 缺省取当前 UTC 时间戳（updated_at 缺省对齐 created_at）。
    resolved_at 由调用方（operations）按 status 决定，本层原样写入。

    Raises:
        sqlite3.IntegrityError: demand_id 重复。
    """
    c_at = created_at or _now_iso()
    u_at = updated_at or c_at
    conn.execute(
        """
        INSERT INTO demands
          (demand_id, title, content, status, priority, project_id,
           origin_idea_id, source_ref, created_at, updated_at, resolved_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            demand_id, title, content, status, priority, project_id,
            origin_idea_id, source_ref, c_at, u_at, resolved_at,
        ),
    )
    conn.commit()
    return demand_id


def update_demand_fields(
    conn: sqlite3.Connection,
    demand_id: str,
    fields: dict[str, Any],
    updated_at: str | None = None,
) -> bool:
    """按白名单更新需求字段（PATCH 语义：只改传入的列）。

    Args:
        conn: memory.db 连接。
        demand_id: 需求主键。
        fields: 待更新列 → 新值。键必须 ⊆ ``EDITABLE_FIELDS``；
            值为 None 表示把该可空列置空（project_id / source_ref 解绑）。
        updated_at: 缺省取当前时间戳。

    Returns:
        True 表示命中并更新；False 表示 demand_id 不存在。

    Raises:
        ValueError: fields 为空或含白名单外的列名（防注入 + 防越权改 status）。
    """
    if not fields:
        raise ValueError("fields 不能为空")
    unknown = [k for k in fields if k not in EDITABLE_FIELDS]
    if unknown:
        raise ValueError(f"不可更新的列: {unknown}")

    u_at = updated_at or _now_iso()
    sets = [f"{col}=?" for col in fields]
    params: list[Any] = list(fields.values())
    sets.append("updated_at=?")
    params.append(u_at)
    params.append(demand_id)

    cur = conn.execute(
        f"UPDATE demands SET {', '.join(sets)} WHERE demand_id=?",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


def update_demand_status(
    conn: sqlite3.Connection,
    demand_id: str,
    status: str,
    resolved_at: str | None = None,
    updated_at: str | None = None,
) -> bool:
    """更新需求状态（同事务写 status + updated_at + resolved_at）。

    ⚠️ resolved_at 传入什么就写什么（含 None=清空）——「转 done 落值 / 转出 done 清空」
    的判断在 operations 层，本层不臆测，保证 data 层无业务语义。

    Returns:
        True 表示命中；False 表示 demand_id 不存在。
    """
    u_at = updated_at or _now_iso()
    cur = conn.execute(
        "UPDATE demands SET status=?, resolved_at=?, updated_at=? WHERE demand_id=?",
        (status, resolved_at, u_at, demand_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_demand(conn: sqlite3.Connection, demand_id: str) -> bool:
    """物理删除一条需求（CRUD 完整性用；当前无 HTTP 端点暴露）。

    ⚠️ 池子类数据的删除策略按设计文档 §4 应为软删除（标记 discarded 可恢复），
    但 0.8 的 demands.status 枚举无 discarded 值，故 HTTP 层不开放删除入口。
    本函数仅供运维/测试与将来软删除落地前的兜底使用。

    Returns:
        True 表示删除了一行；False 表示 demand_id 不存在。
    """
    cur = conn.execute("DELETE FROM demands WHERE demand_id=?", (demand_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------- 读取 ----------

def get_demand(conn: sqlite3.Connection, demand_id: str) -> dict | None:
    """返回单条需求 dict；不存在返回 None。"""
    cur = conn.execute(
        f"SELECT {_COLUMNS} FROM demands WHERE demand_id=?", (demand_id,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(row, cur)


def _build_filters(
    statuses: list[str] | None,
    project_id: str | None,
    q: str | None,
    since: str | None,
    until: str | None,
    time_field: str,
) -> tuple[list[str], list[Any]]:
    """构造 WHERE 子句片段与参数（list/count 共用，保证两者过滤条件严格一致）。"""
    if time_field not in TIME_FIELDS:
        raise ValueError(f"非法时间过滤列: {time_field}")

    where: list[str] = []
    params: list[Any] = []

    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        where.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if project_id is not None:
        where.append("project_id=?")
        params.append(project_id)
    if q:
        pattern = _like_pattern(q)
        where.append(r"(title LIKE ? ESCAPE '\' OR content LIKE ? ESCAPE '\')")
        params.extend([pattern, pattern])
    if since:
        where.append(f"{time_field} >= ?")
        params.append(since)
    if until:
        where.append(f"{time_field} <= ?")
        params.append(until)

    return where, params


def list_demands(
    conn: sqlite3.Connection,
    statuses: list[str] | None = None,
    project_id: str | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    limit: int = 50,
) -> tuple[list[dict], int]:
    """分页查询需求池。

    Args:
        conn: memory.db 连接。
        statuses: 状态多值过滤（None/空 = 不过滤，即"全部状态可见"）。
        project_id: 精确匹配关联项目。
        q: 标题/内容子串（通配符已转义）。
        since / until: 时间范围（闭区间）；作用列 = sort 为时间列时取 sort，
            否则（sort=priority）回落 updated_at——按 priority 排序时对优先级做
            时间范围过滤无意义，回落到"最近更新"是唯一合理解释。
        sort: ``priority`` / ``updated_at`` / ``created_at``。
        order: ``asc`` / ``desc``。
        page: 页码，从 1 起。
        limit: 页大小。

    Returns:
        ``(items, total)``：当前页条目 + 匹配过滤条件的总数（total 用于分页信封）。

    Raises:
        ValueError: sort / order 越出白名单，或 page / limit 非正
            （业务级 400 由 operations 层提前拦截，此处是防注入兜底）。
    """
    if sort not in SORT_FIELDS:
        raise ValueError(f"非法排序字段: {sort}")
    if order not in ORDER_DIRECTIONS:
        raise ValueError(f"非法排序方向: {order}")
    if page < 1 or limit < 1:
        raise ValueError(f"非法分页参数: page={page} limit={limit}")

    time_field = sort if sort in TIME_FIELDS else "updated_at"
    where, params = _build_filters(statuses, project_id, q, since, until, time_field)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    total_cur = conn.execute(f"SELECT COUNT(*) AS c FROM demands{where_sql}", params)
    total_row = total_cur.fetchone()
    total = int(total_row[0]) if total_row is not None else 0

    direction = "DESC" if order == "desc" else "ASC"
    # demand_id 兜底排序键：priority/时间戳可能同值，无确定性次序会导致翻页重复或漏条
    sql = (
        f"SELECT {_COLUMNS} FROM demands{where_sql} "
        f"ORDER BY {sort} {direction}, demand_id ASC LIMIT ? OFFSET ?"
    )
    cur = conn.execute(sql, [*params, limit, (page - 1) * limit])
    return [_row_to_dict(r, cur) for r in cur.fetchall()], total


def count_demands(conn: sqlite3.Connection, status: str | None = None) -> int:
    """按状态计数需求（status=None 计全部）。"""
    if status is None:
        cur = conn.execute("SELECT COUNT(*) AS c FROM demands")
    else:
        cur = conn.execute("SELECT COUNT(*) AS c FROM demands WHERE status=?", (status,))
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def list_demands_by_origin_idea(
    conn: sqlite3.Connection, origin_idea_id: str
) -> list[dict]:
    """按升格来源创意反查需求（ST-14 溯源链闭合：这条创意升格成了哪些需求）。

    走索引 ``idx_demands_origin_idea``；按 created_at 升序（升格先后顺序）。
    """
    cur = conn.execute(
        f"SELECT {_COLUMNS} FROM demands WHERE origin_idea_id=? "
        "ORDER BY created_at ASC, demand_id ASC",
        (origin_idea_id,),
    )
    return [_row_to_dict(r, cur) for r in cur.fetchall()]


# ---------- project_meta 软校验原料（ST-16 并行开发，表可能尚不存在） ----------

def project_meta_available(conn: sqlite3.Connection) -> bool:
    """探测 memory.db 中 project_meta 表是否已就绪（ST-16 交付物）。

    需求池不对 project_id 加外键（理由见 db.py `_migrate_demands_table`），
    改由 operations 层做**条件软校验**：表在 → 校验 project_id 存在性；
    表不在（ST-16 未合并的过渡期）→ 跳过校验，照常存值。
    这样合并后校验自动生效，无需回改需求池代码。
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='project_meta'"
    )
    return cur.fetchone() is not None


def project_exists(conn: sqlite3.Connection, project_id: str) -> bool:
    """project_meta 中是否存在该 project_id（表不存在时一律返回 False）。

    调用方必须先用 `project_meta_available` 判定表就绪，否则无法区分
    "表没建"与"项目不存在"两种语义。
    """
    if not project_meta_available(conn):
        return False
    cur = conn.execute(
        "SELECT 1 FROM project_meta WHERE project_id=? LIMIT 1", (project_id,)
    )
    return cur.fetchone() is not None
