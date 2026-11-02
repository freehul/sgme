# -*- coding: utf-8 -*-
"""skill_meta_dao.py：技能生命周期状态数据层（T-401）。

职责：把「技能现在算不算数」这件事从技能索引行里独立出来——
``active``（在役）/ ``deprecated``（软删宽限期，可恢复）/ ``tombstone``（改名墓碑，
``superseded_by`` 指向新名）。语义与 ``sgme/skills/store.py`` 的写侧治理对齐：
``remove_skill(soft=True)`` 落 ``deprecated``，``rename_skill`` 给旧名留
``superseded_by`` 墓碑；本表是这两类状态的**库内索引**，供检索侧过滤与对账使用。

设计要点：
- **独立表 skill_meta，不往 skills 主表加列**：技能行会被增量同步按 sha 重写、
  被 delete 分支清理；生命周期状态必须在索引行消失后仍然存续（墓碑名的价值
  恰恰在技能行没了之后）。同时保证 skills / skill_vectors / skills_fts 现有
  列零改动（向后兼容）。
- **未登记 = active**：新技能默认在役，无需预写行；``get_status`` 返回 None
  时调用方按 ``STATUS_ACTIVE`` 处理（与 wiki_pages 的 status 缺省 'active' 同义）。
- **白名单**：active / deprecated / tombstone，非法值一律 ``ValueError``
  （表上另有 CHECK 约束兜底直接写 SQL 的路径）。DAOs 是唯一写入口。
- **幂等**：set_status 走 upsert，重复写同一状态不报错（只刷新 updated_at）。

分层：只做 SQL 与校验，不 import fastapi/mcp（data 层不认识协议）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

STATUS_ACTIVE = "active"
STATUS_DEPRECATED = "deprecated"
STATUS_TOMBSTONE = "tombstone"

# 状态白名单（顺序 = 生命周期推进方向：在役 → 软删 → 墓碑）
VALID_STATUSES: frozenset[str] = frozenset(
    {STATUS_ACTIVE, STATUS_DEPRECATED, STATUS_TOMBSTONE}
)


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（与 skill_package_dao 同口径）。"""
    return datetime.now(UTC).isoformat()


def _rows_to_dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    """游标行 → dict 列表（按 cursor.description 组键，不依赖连接级 row_factory）。"""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _check_name(name: str) -> str:
    """校验技能名：非空字符串，返回去空白后的值。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"非法技能名: {name!r}")
    return name.strip()


def validate_status(status: str) -> str:
    """校验状态白名单（active / deprecated / tombstone），非法值抛 ValueError。"""
    if status not in VALID_STATUSES:
        raise ValueError(
            f"非法技能状态: {status!r}（合法值: {sorted(VALID_STATUSES)}）"
        )
    return status


def set_status(
    conn: sqlite3.Connection,
    name: str,
    status: str,
    superseded_by: str | None = None,
) -> dict[str, Any]:
    """写入/更新技能生命周期状态（upsert，幂等）。

    Args:
        name: 技能名（墓碑名同样允许——正是技能行被清理后仍需留档的场景）。
        status: 白名单 active / deprecated / tombstone，非法值 ValueError。
        superseded_by: 继任技能名；tombstone 通常必填（由调用方业务约束），
            非墓碑状态传 None 即清空指向。

    Returns:
        落库后的整行 dict（name / status / superseded_by / updated_at）。
    """
    name = _check_name(name)
    status = validate_status(status)
    successor: str | None = None
    if superseded_by is not None:
        successor = _check_name(superseded_by)
        if successor == name:
            raise ValueError(f"superseded_by 不能指向自身: {name!r}")
    conn.execute(
        """
        INSERT INTO skill_meta (name, status, superseded_by, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET
          status = excluded.status,
          superseded_by = excluded.superseded_by,
          updated_at = excluded.updated_at
        """,
        (name, status, successor, _now_iso()),
    )
    conn.commit()
    row = get_meta(conn, name)
    assert row is not None  # 刚 upsert 过，必然存在
    return row


def get_status(conn: sqlite3.Connection, name: str) -> str | None:
    """读状态；未登记返回 None（调用方按 active 处理）。"""
    row = conn.execute(
        "SELECT status FROM skill_meta WHERE name=?", (_check_name(name),)
    ).fetchone()
    return row[0] if row else None


def get_meta(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """读完整生命周期行（含 superseded_by）；未登记返回 None。"""
    cur = conn.execute(
        "SELECT name, status, superseded_by, updated_at FROM skill_meta WHERE name=?",
        (_check_name(name),),
    )
    rows = _rows_to_dicts(cur)
    return rows[0] if rows else None


def list_by_status(
    conn: sqlite3.Connection, status: str, limit: int | None = None
) -> list[dict[str, Any]]:
    """按状态列技能（name 升序），返回整行 dict 列表；非法状态 ValueError。

    只返回**显式登记**过该状态的行——新技能默认在役但不在本表，"未登记=active"
    的语义只体现在 get_status 侧；需要「全量在役清单」的调用方应以 skills 表
    减去 deprecated/tombstone 名单，而非依赖 list_by_status('active')。
    limit=None 返全部（生产技能量级小，对账用全量更直观）。
    """
    status = validate_status(status)
    sql = (
        "SELECT name, status, superseded_by, updated_at FROM skill_meta"
        " WHERE status=? ORDER BY name ASC"
    )
    params: list[Any] = [status]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return _rows_to_dicts(conn.execute(sql, params))
