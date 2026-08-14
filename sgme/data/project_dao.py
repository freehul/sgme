"""data/project_dao.py：memory.db 的项目注册表 DAO（project_meta，0.8 ST-16）。

唯一操作 `project_meta` 表的入口（铁律 B30：data 是唯一数据库操作层）。
operations / server 只调本模块函数，**不拼 SQL**。

表定位（`SGME-创意池与需求池设计-v0.1.md` §3 ③「项目注册表（轻量元数据）」）：
项目名 | 路径 | git 仓库 | 最近活跃 | 当前里程碑。刻意保持轻量——
项目本体（代码 / issues / 设计文档）在项目目录由 git 管理，SGME 只存
「关于项目的事实」，不做项目管理系统。

函数清单：
- ``upsert_project``  登记（不存在则插入、存在则更新）——project_init 可重复执行
- ``get_project``     单条查询
- ``list_projects``   分页 + 名称子串 / 里程碑过滤 + 白名单排序
- ``count_projects``  同条件总数（分页信封 total）
- ``update_project``  部分字段更新（PATCH 语义，显式 None 可清空可空列）

两种写语义的区别（**勿混用**）：
- ``upsert_project``：None = 「本次未提供」→ 保留原值（COALESCE 语义），
  因为 project_init 重复登记时不该把上次补录的 git_repo / milestone 抹掉。
- ``update_project``：入参 dict 里出现的键就是「本次显式提供」→ 原样写入，
  显式 None 表示清空该列（仅限可空列）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

# 排序字段白名单（拼进 ORDER BY，必须白名单化以杜绝 SQL 注入）。
# operations 层同样校验一遍并回 400；此处是 data 层的最后一道防线。
SORT_FIELDS: tuple[str, ...] = ("last_active_at", "updated_at", "created_at")

# PATCH 可更新列（project_id 是主键不可改；created_at 不可改）
UPDATABLE_FIELDS: tuple[str, ...] = (
    "name",
    "path",
    "git_repo",
    "last_active_at",
    "milestone",
)


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（与 db.py / 其余 DAO 同格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _like_pattern(raw: str) -> str:
    """把用户子串转为 LIKE 模式串，转义 LIKE 元字符（配合 ``ESCAPE '\\'``）。"""
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """sqlite3.Row → dict（None 透传）。"""
    return dict(row) if row is not None else None


# ---------- 读 ----------

def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    """返回单条项目元数据；不存在返回 None。"""
    cur = conn.execute(
        "SELECT * FROM project_meta WHERE project_id=?", (project_id,)
    )
    return _row_to_dict(cur.fetchone())


def _build_filters(
    q: str | None,
    milestone: str | None,
) -> tuple[str, list[Any]]:
    """构造 WHERE 子句与参数（无条件时返回空串）。

    Args:
        q: 名称子串，同时匹配 project_id 与 name（大小写不敏感，LIKE 语义）。
        milestone: 里程碑精确过滤。

    Returns:
        ``(where_sql, params)``，where_sql 形如 `" WHERE ... AND ..."` 或 `""`。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if q:
        pattern = _like_pattern(q)
        clauses.append(
            "(project_id LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern])
    if milestone:
        clauses.append("milestone = ?")
        params.append(milestone)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def count_projects(
    conn: sqlite3.Connection,
    q: str | None = None,
    milestone: str | None = None,
) -> int:
    """同过滤条件下的总条数（分页信封 total）。"""
    where_sql, params = _build_filters(q, milestone)
    cur = conn.execute(f"SELECT COUNT(*) AS cnt FROM project_meta{where_sql}", params)
    row = cur.fetchone()
    return int(row["cnt"]) if row is not None else 0


def list_projects(
    conn: sqlite3.Connection,
    q: str | None = None,
    milestone: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """分页列出项目元数据。

    排序：``sort`` 必须命中 SORT_FIELDS 白名单（否则回落 updated_at），
    NULL 值恒排在最后（``last_active_at`` 可为空，NULL 混排会让列表看起来乱序），
    并以 project_id 升序兜底保证分页稳定（时间戳秒级精度存在同值可能）。

    Args:
        conn: memory.db 连接。
        q: 名称子串过滤（project_id / name）。
        milestone: 里程碑精确过滤。
        sort: last_active_at / updated_at / created_at。
        order: desc / asc。
        page: 页码，≥ 1。
        limit: 页大小，≥ 1。

    Returns:
        项目 dict 列表（列与 project_meta 一致）。
    """
    sort_col = sort if sort in SORT_FIELDS else "updated_at"
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    page_num = max(1, int(page))
    page_size = max(1, int(limit))
    offset = (page_num - 1) * page_size

    where_sql, params = _build_filters(q, milestone)
    sql = (
        f"SELECT * FROM project_meta{where_sql} "
        f"ORDER BY {sort_col} IS NULL, {sort_col} {direction}, project_id ASC "
        f"LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, [*params, page_size, offset]).fetchall()
    return [dict(r) for r in rows]


# ---------- 写 ----------

def upsert_project(
    conn: sqlite3.Connection,
    project_id: str,
    name: str | None = None,
    path: str | None = None,
    git_repo: str | None = None,
    last_active_at: str | None = None,
    milestone: str | None = None,
    now: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """登记项目（不存在则插入，存在则更新）。

    幂等语义：`scripts/project_init.py` 立项流程可能被重复执行（重跑 / 补登），
    同名项目二次登记应当是「更新」而不是报错，因此本函数是 upsert 而非 insert。

    None 语义：本次未提供的字段**保留原值**（COALESCE），不会被抹成 NULL。
    需要清空某列请走 ``update_project``（PATCH 语义）。

    Args:
        conn: memory.db 连接。
        project_id: 项目名（纯英文，与 D:\\Projects 目录名一致），主键。
        name: 展示名，缺省与 project_id 同值（冗余列，便于将来改名迁移）。
        path: 项目绝对路径。新建时必填（NOT NULL 列），更新时可省。
        git_repo: git 仓库地址（本地路径或远端 URL）。
        last_active_at: 最近活跃时刻（ISO8601）。
        milestone: 当前里程碑（如 v1.0）。
        now: 时间戳注入点（测试用）；缺省取当前 UTC。

    Returns:
        ``(project_row, created)``：落库后的完整行 + 本次是否为新建。
    """
    ts = now or _now_iso()
    existing = get_project(conn, project_id)
    created = existing is None

    if created:
        conn.execute(
            """
            INSERT INTO project_meta
              (project_id, name, path, git_repo, last_active_at, milestone,
               created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                project_id,
                name or project_id,
                path or "",
                git_repo,
                last_active_at,
                milestone,
                ts,
                ts,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE project_meta
            SET name           = COALESCE(?, name),
                path           = COALESCE(?, path),
                git_repo       = COALESCE(?, git_repo),
                last_active_at = COALESCE(?, last_active_at),
                milestone      = COALESCE(?, milestone),
                updated_at     = ?
            WHERE project_id = ?
            """,
            (name, path, git_repo, last_active_at, milestone, ts, project_id),
        )
    conn.commit()

    row = get_project(conn, project_id)
    # 主键写入后立刻回读，理论上不可能为 None；防御性兜底给出可读错误。
    if row is None:  # pragma: no cover - 仅防御
        raise sqlite3.DatabaseError(f"project_meta 写入后回读失败: {project_id}")
    return row, created


def update_project(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any] | None:
    """部分字段更新（PATCH 语义）。项目不存在返回 None。

    与 upsert 的差别：``fields`` 里出现的键即「显式提供」，原样写入——
    显式传 None 表示**清空**该列（仅可空列 git_repo / last_active_at / milestone
    有意义；name / path 为 NOT NULL 列，由 operations 层拦截空值）。
    ``updated_at`` 恒刷新（写操作必须更新时间戳）。

    Args:
        conn: memory.db 连接。
        project_id: 目标项目主键。
        fields: 待更新字段，键必须 ⊆ UPDATABLE_FIELDS（非法键由 operations 层拦截）。
        now: 时间戳注入点（测试用）。

    Returns:
        更新后的完整行；项目不存在返回 None。
    """
    if get_project(conn, project_id) is None:
        return None

    ts = now or _now_iso()
    sets: list[str] = []
    params: list[Any] = []
    for key in UPDATABLE_FIELDS:          # 按白名单顺序遍历，杜绝外部键名拼进 SQL
        if key in fields:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    sets.append("updated_at = ?")
    params.append(ts)
    params.append(project_id)

    conn.execute(
        f"UPDATE project_meta SET {', '.join(sets)} WHERE project_id = ?", params
    )
    conn.commit()
    return get_project(conn, project_id)
