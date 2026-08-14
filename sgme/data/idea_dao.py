"""data/idea_dao.py：创意池 DAO（T-56 维度独立日：ideas 独立表版）。

历史：0.8 ST-14 时代创意 = 「带 ideas 维度的记忆 + ttl_days=NULL」，本模块
在 memories 上做约束读写；2026-08-14「维度独立日」（T-56）独立为 ideas 表——
创意完全由用户/接入 agent 掌控（idea_add / /v1/admin/ideas*），LLM 提炼不再写创意。

与 memory_dao 的分工：memory_dao 是通用记忆 DAO（提炼链路写入 / 模板查询）；
创意池是**人工修正**场景，语义与调用面完全不同（追加式备注、自由文本标记、
分页浏览），故独立成文件。本模块**只读写 ideas 表**，不碰 memories。

铁律
----
1. **软删除**：`status='rejected'`（+ `rejected_at` / `reject_reason`），
   **绝不物理删除**。理由见 `routes_ideas` 模块文档。
2. **备注追加式**：`notes` 是 JSON 数组，写入前先读现值再 append，
   **绝不覆盖**既有备注。
3. 所有写操作刷新 `updated_at`（创意长期保存，无 TTL 续期副作用）。
4. 无 content_seg/FTS：创意不进 /v1/search，列表 q 过滤走 LIKE 子串。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

# 创意维度注册表 id（维度注册表保留该维度以兼容旧 memories 数据检索；
# 新创意全部落 ideas 表，不再写 memories/memory_tags）
IDEAS_DIMENSION_ID: str = "ideas"

# 排序字段白名单（防 SQL 注入 + 防未知列）；键 = 对外参数名，值 = ideas 列名
SORT_COLUMNS: dict[str, str] = {
    "updated_at": "updated_at",
    "created_at": "created_at",
    "priority": "priority",
}

# 时间型排序字段（since/until 直接作用其上；非时间型回落 updated_at）
TIME_SORT_FIELDS: frozenset[str] = frozenset({"updated_at", "created_at"})

# 创意状态枚举（对齐 memories.status 语义）
VALID_STATUSES: frozenset[str] = frozenset({"active", "rejected", "expired", "archived"})

# 软删除写入 reject_reason 的缺省文案（历史语义：底层=标记 discarded）
DEFAULT_DISCARD_REASON: str = "创意池软删除（discarded）"


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（秒级，与仓库既有 `_now_iso` 完全一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_notes(raw: Any) -> list[dict[str, str]]:
    """把 `ideas.notes` 的原始值解析为备注数组。

    容错策略（读路径绝不因脏数据抛异常）：
    - None / 空串 / 空白串 → `[]`
    - 非法 JSON → `[]`
    - JSON 顶层不是数组 → `[]`
    - 数组元素不是 dict → 跳过该元素

    Returns:
        `[{"ts": "...", "text": "..."}, ...]`，保持落库顺序（即追加顺序）。
    """
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append({"ts": str(item.get("ts", "")), "text": str(item.get("text", ""))})
    return out


def dump_notes(notes: list[dict[str, str]]) -> str:
    """备注数组 → 落库 JSON 文本（中文不转义，便于人工直接读库排障）。"""
    return json.dumps(notes, ensure_ascii=False)


def _like_pattern(q: str) -> str:
    r"""把用户子串转为 LIKE 模式，转义 LIKE 元字符（配合 ``ESCAPE '\'``）。

    不转义会让用户输入的 `%` / `_` 变成通配符，造成越权范围匹配。
    """
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _build_filters(
    *,
    statuses: Iterable[str] | None,
    custom_flag: str | None,
    has_flag: bool | None,
    q: str | None,
    since: str | None,
    until: str | None,
    sort: str,
) -> tuple[str, list[Any]]:
    """拼装 WHERE 子句与参数（ideas 表，无维度 JOIN——创意即本表全部）。"""
    clauses: list[str] = []
    params: list[Any] = []

    status_list = [s for s in (statuses or []) if s]
    if status_list:
        placeholders = ",".join("?" * len(status_list))
        clauses.append(f"status IN ({placeholders})")
        params.extend(status_list)

    if custom_flag is not None:
        clauses.append("custom_flag = ?")
        params.append(custom_flag)

    if has_flag is True:
        clauses.append("custom_flag IS NOT NULL AND custom_flag != ''")
    elif has_flag is False:
        clauses.append("(custom_flag IS NULL OR custom_flag = '')")

    if q:
        clauses.append("content LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(q))

    time_col = sort if sort in TIME_SORT_FIELDS else "updated_at"
    if since:
        clauses.append(f"{time_col} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{time_col} <= ?")
        params.append(until)

    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


def count_ideas(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[str] | None = ("active",),
    custom_flag: str | None = None,
    has_flag: bool | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sort: str = "updated_at",
) -> int:
    """统计满足过滤条件的创意总数（分页信封的 `total`）。"""
    where_sql, params = _build_filters(
        statuses=statuses, custom_flag=custom_flag,
        has_flag=has_flag, q=q, since=since, until=until, sort=sort,
    )
    row = conn.execute(f"SELECT COUNT(*) AS c FROM ideas{where_sql}", params).fetchone()
    return int(row["c"]) if row else 0


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    """ideas 行 → 创意条目响应形态（字段位对齐既有契约 §5.3.3，ID 语义=idea_id）。"""
    return {
        "idea_id": row["idea_id"],
        "content": row["content"],
        "priority": row["priority"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "notes": parse_notes(row["notes"]),
        "custom_flag": row["custom_flag"],
        "reject_reason": row["reject_reason"],
        "rejected_at": row["rejected_at"],
        "source_ref": row["source_ref"],
        "origin_memory_id": row["origin_memory_id"],
    }


def list_ideas(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[str] | None = ("active",),
    custom_flag: str | None = None,
    has_flag: bool | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按过滤条件分页列出创意条目（ideas 表）。

    Args:
        conn: memory.db 连接。
        statuses: 状态白名单，缺省仅 `active`（软删除条目默认不可见）。
        custom_flag: 人工标记精确匹配。
        has_flag: True 仅有标记 / False 仅无标记 / None 不过滤。
        q: 内容子串。
        since / until: 时间窗（作用于 sort 时间列，非时间列回落 updated_at）。
        sort: 排序字段，须为 `SORT_COLUMNS` 的键（调用方先校验）。
        order: `asc` / `desc`（调用方先校验）。
        page: 页码，≥ 1。
        limit: 页大小。

    Returns:
        条目列表（形态见 `_row_to_item`）。
    """
    col = SORT_COLUMNS.get(sort, "updated_at")
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    where_sql, params = _build_filters(
        statuses=statuses, custom_flag=custom_flag,
        has_flag=has_flag, q=q, since=since, until=until, sort=sort,
    )
    offset = max(0, (max(1, page) - 1) * max(1, limit))
    # 次级排序键 idea_id：主键唯一，保证同值排序稳定（分页不跳条/不重条）
    sql = (
        f"SELECT * FROM ideas{where_sql} "
        f"ORDER BY {col} {direction}, idea_id {direction} "
        f"LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    return [_row_to_item(r) for r in rows]


def get_idea(conn: sqlite3.Connection, idea_id: str) -> dict[str, Any] | None:
    """读取单条创意。

    Args:
        conn: memory.db 连接。
        idea_id: 创意 id。

    Returns:
        条目字典，或 None（不存在）。
    """
    row = conn.execute("SELECT * FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()
    if row is None:
        return None
    return _row_to_item(row)


def update_idea_content(
    conn: sqlite3.Connection,
    idea_id: str,
    *,
    content: str | None = None,
    priority: int | None = None,
    updated_at: str | None = None,
) -> bool:
    """编辑创意内容 / 优先级（人工完善是创意池的核心环节，设计 §5）。

    Args:
        conn: memory.db 连接。
        idea_id: 创意 id。
        content: 新内容；None 表示不改。
        priority: 新优先级；None 表示不改。
        updated_at: 显式时间戳（缺省取当前 UTC）。

    Returns:
        True = 有行被更新；False = 创意不存在。
    """
    sets: list[str] = []
    params: list[Any] = []
    if content is not None:
        sets.append("content=?")
        params.append(content)
    if priority is not None:
        sets.append("priority=?")
        params.append(int(priority))
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(updated_at or _now_iso())
    params.append(idea_id)
    cur = conn.execute(f"UPDATE ideas SET {', '.join(sets)} WHERE idea_id=?", params)
    conn.commit()
    return cur.rowcount > 0


def append_idea_note(
    conn: sqlite3.Connection,
    idea_id: str,
    text: str,
    *,
    ts: str | None = None,
) -> list[dict[str, str]] | None:
    """**追加式**写入一条人工备注（绝不覆盖既有备注，设计 §4）。

    读-改-写在单个 `BEGIN IMMEDIATE` 事务内完成：立刻拿写锁，
    杜绝两个并发追加互相覆盖（丢失更新）。

    Args:
        conn: memory.db 连接。
        idea_id: 创意 id。
        text: 备注正文（调用方已校验非空）。
        ts: 显式时间戳（缺省取当前 UTC）；仅测试与回填场景传入。

    Returns:
        追加后的完整备注数组；创意不存在时返回 None。
    """
    stamp = ts or _now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT notes FROM ideas WHERE idea_id=?", (idea_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        notes = parse_notes(row["notes"])
        notes.append({"ts": stamp, "text": text})
        conn.execute(
            "UPDATE ideas SET notes=?, updated_at=? WHERE idea_id=?",
            (dump_notes(notes), stamp, idea_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return notes


def set_idea_flag(
    conn: sqlite3.Connection,
    idea_id: str,
    custom_flag: str | None,
    *,
    updated_at: str | None = None,
) -> bool:
    """设置人工标记（自由文本，**无固定枚举**——用户说了算，设计 §4/§5）。

    Args:
        conn: memory.db 连接。
        idea_id: 创意 id。
        custom_flag: 标记文本；None 表示清除标记。
        updated_at: 显式时间戳（缺省取当前 UTC）。

    Returns:
        True = 有行被更新；False = 创意不存在。
    """
    cur = conn.execute(
        "UPDATE ideas SET custom_flag=?, updated_at=? WHERE idea_id=?",
        (custom_flag, updated_at or _now_iso(), idea_id),
    )
    conn.commit()
    return cur.rowcount > 0


def soft_delete_idea(
    conn: sqlite3.Connection,
    idea_id: str,
    *,
    reason: str | None = None,
    updated_at: str | None = None,
) -> bool:
    """软删除：`status='rejected'`（**绝不物理删除**，设计 §4「可恢复」）。

    `custom_flag` **刻意不动**——它是用户自有的自由文本标记，
    系统删除动作占用它会覆盖用户数据（如「升格」标记）。
    「discarded」语义落在 `reject_reason` 上，保留审计痕迹。

    Args:
        conn: memory.db 连接。
        idea_id: 创意 id。
        reason: 删除说明；缺省 `DEFAULT_DISCARD_REASON`。
        updated_at: 显式时间戳（缺省取当前 UTC）。

    Returns:
        True = 有行被更新；False = 创意不存在。
    """
    stamp = updated_at or _now_iso()
    cur = conn.execute(
        "UPDATE ideas SET status='rejected', rejected_at=?, reject_reason=?, updated_at=? "
        "WHERE idea_id=?",
        (stamp, reason or DEFAULT_DISCARD_REASON, stamp, idea_id),
    )
    conn.commit()
    return cur.rowcount > 0


def restore_idea(
    conn: sqlite3.Connection,
    idea_id: str,
    *,
    updated_at: str | None = None,
) -> bool:
    """撤销软删除：恢复 `status='active'`（清空 rejected_at / reject_reason）。

    Args:
        conn: memory.db 连接。
        idea_id: 创意 id。
        updated_at: 显式时间戳（缺省取当前 UTC）。

    Returns:
        True = 有行被更新；False = 创意不存在。
    """
    cur = conn.execute(
        "UPDATE ideas SET status='active', rejected_at=NULL, reject_reason=NULL, "
        "updated_at=? WHERE idea_id=?",
        (updated_at or _now_iso(), idea_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------- 人工添加（2026-08-13 用户定：创意由用户主动提出，不再 LLM 自动打标） ----------

def _uuid() -> str:
    """新创意 id（与 memory_dao._uuid 同构，避免跨模块依赖）。"""
    import uuid

    return str(uuid.uuid4())


def add_idea(
    conn: sqlite3.Connection,
    content: str,
    *,
    priority: int = 50,
    source_ref: str | None = None,
    created_at: str | None = None,
) -> str:
    """人工新增一条创意（独立 ideas 表，T-56）。

    - 铁律对齐：创意长期保存（无 TTL 概念）；`status='active'`
    - 溯源 ``source_ref`` 可选（如「用户对话 2026-08-13」或外部链接）

    Args:
        conn: memory.db 连接。
        content: 创意正文（调用方已校验非空）。
        priority: 优先级 0-100，缺省 50。
        source_ref: 人工溯源引用（可选）。
        created_at: 显式时间戳（缺省取当前 UTC）。

    Returns:
        新创意 idea_id。
    """
    iid = _uuid()
    stamp = created_at or _now_iso()
    conn.execute(
        """
        INSERT INTO ideas (idea_id, content, priority, status, source_ref,
                           created_at, updated_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (iid, content, int(priority), "active", source_ref, stamp, stamp),
    )
    conn.commit()
    return iid
