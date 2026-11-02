# -*- coding: utf-8 -*-
"""sgme/data/skill_candidates_dao.py：技能候选池 DAO（2.0 D-67 消费端）。

定位
----
``memories.route='skill_candidate'`` 的记忆是「可复用经验」——它们不进画像
（``profile/inject.py`` 只取 route=memory），此前也没有任何消费端，等于黑洞。
本表是这些记忆的**消费台账**：一条记忆最多出现一次（memory_id UNIQUE），
审核状态机 pending → accepted / ignored / promoted。

为什么单独建表而不是 ``skills`` 加 status='candidate'
---------------------------------------------------
1. ``operations.skills.sync_index`` 以磁盘 source_dirs 为真源，delete 分支会把
   「库里存在、源目录没有」的技能行删掉——候选行会被下一次同步直接清空；
2. ``skills_fts`` 触发器会把候选正文写进技能全文索引，未审核草稿会污染
   ``skill_search``；
3. 候选的生命周期（待审/忽略）与正式技能（active/tombstone）不同，混表会让
   ``list_skills`` / 依赖门禁 / 缓存版本都要额外排除候选。

本模块只管 SQL；置信度、草稿生成、幂等语义在
``sgme/operations/skill_candidates.py``。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

VALID_STATUSES: tuple[str, ...] = ("pending", "accepted", "ignored", "promoted")


def _now() -> str:
    """UTC ISO 8601 时间戳（与仓库其余 DAO 同格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_candidate(row: sqlite3.Row) -> dict[str, Any]:
    """库行 → 候选字典（tags JSON 还原为列表）。"""
    d = dict(row)
    tags = d.get("tags")
    if isinstance(tags, str):
        try:
            d["tags"] = json.loads(tags)
        except Exception:  # noqa: BLE001 历史脏数据按逗号串回退
            d["tags"] = [t for t in tags.split(",") if t]
    elif tags is None:
        d["tags"] = []
    if d.get("confidence") is not None:
        d["confidence"] = float(d["confidence"])
    return d


def create_candidate(
    conn: sqlite3.Connection, rec: dict[str, Any]
) -> tuple[int, bool]:
    """插入一条技能候选（幂等，按 memory_id 唯一）。

    Args:
        conn: skills.db 连接。
        rec: 至少含 memory_id / name；其余字段可选。

    Returns:
        ``(candidate_id, created)``——created=False 表示该 memory_id 已消费过，
        本次为 no-op（不会覆盖既有审核状态）。
    """
    tags = rec.get("tags") or []
    tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else str(tags)
    now = _now()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO skill_candidates
          (memory_id, name, description, content, category, tags, confidence,
           status, reason, note, reviewer, source, content_sha256,
           created_at, updated_at, reviewed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            rec["memory_id"],
            rec["name"],
            rec.get("description") or "",
            rec.get("content") or "",
            rec.get("category") or None,
            tags_json,
            float(rec.get("confidence") or 0.0),
            rec.get("status") or "pending",
            rec.get("reason") or None,
            rec.get("note") or None,
            rec.get("reviewer") or None,
            rec.get("source") or "memory_route",
            rec.get("content_sha256") or None,
            now,
            now,
            rec.get("reviewed_at") or None,
        ),
    )
    created = cur.rowcount == 1
    row = conn.execute(
        "SELECT candidate_id FROM skill_candidates WHERE memory_id=?",
        (rec["memory_id"],),
    ).fetchone()
    return (int(row["candidate_id"]) if row else 0), created


def get_candidate(conn: sqlite3.Connection, memory_id: str) -> dict[str, Any] | None:
    """按记忆 id 取候选；不存在返回 None。"""
    row = conn.execute(
        "SELECT * FROM skill_candidates WHERE memory_id=?", (memory_id,)
    ).fetchone()
    return _row_to_candidate(row) if row else None


def find_by_content_sha(
    conn: sqlite3.Connection, content_sha256: str
) -> dict[str, Any] | None:
    """按正文指纹找已有候选（跨记忆去重用）；空指纹不参与匹配。"""
    if not content_sha256:
        return None
    row = conn.execute(
        "SELECT * FROM skill_candidates WHERE content_sha256=? "
        "ORDER BY candidate_id LIMIT 1",
        (content_sha256,),
    ).fetchone()
    return _row_to_candidate(row) if row else None


def candidate_memory_ids(conn: sqlite3.Connection) -> set[str]:
    """已消费过的记忆 id 全集（列表过滤用，量级 = 候选池大小）。"""
    return {
        r["memory_id"]
        for r in conn.execute("SELECT memory_id FROM skill_candidates").fetchall()
    }


def list_candidates(
    conn: sqlite3.Connection,
    status: str | None = None,
    min_confidence: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """候选池分页列表（按置信度降序、创建时间降序）。"""
    sql = "SELECT * FROM skill_candidates"
    conds: list[str] = []
    params: list[Any] = []
    if status:
        conds.append("status=?")
        params.append(status)
    if min_confidence is not None:
        conds.append("confidence >= ?")
        params.append(float(min_confidence))
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY confidence DESC, candidate_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [_row_to_candidate(r) for r in conn.execute(sql, params).fetchall()]


def count_candidates(
    conn: sqlite3.Connection,
    status: str | None = None,
    min_confidence: float | None = None,
) -> int:
    """候选池计数（过滤口径与 list_candidates 一致）。"""
    sql = "SELECT COUNT(*) AS n FROM skill_candidates"
    conds: list[str] = []
    params: list[Any] = []
    if status:
        conds.append("status=?")
        params.append(status)
    if min_confidence is not None:
        conds.append("confidence >= ?")
        params.append(float(min_confidence))
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    return int(conn.execute(sql, params).fetchone()["n"])


def update_status(
    conn: sqlite3.Connection,
    memory_id: str,
    status: str,
    note: str | None = None,
    reviewer: str | None = None,
) -> bool:
    """更新审核状态；返回是否命中行。

    幂等约定由 operations 层保证（同状态不重复写）；本函数只负责落库。
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"非法候选状态: {status!r}（合法值: {VALID_STATUSES}）")
    now = _now()
    cur = conn.execute(
        """
        UPDATE skill_candidates
           SET status=?, note=COALESCE(?, note), reviewer=COALESCE(?, reviewer),
               updated_at=?, reviewed_at=?
         WHERE memory_id=?
        """,
        (status, note, reviewer, now, now, memory_id),
    )
    return cur.rowcount == 1


def summary(conn: sqlite3.Connection, high_threshold: float = 0.8) -> dict[str, Any]:
    """候选池总览：{total, by_status, high_confidence}。"""
    by_status = {
        r["status"]: int(r["n"])
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM skill_candidates GROUP BY status"
        ).fetchall()
    }
    total = sum(by_status.values())
    high = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM skill_candidates WHERE confidence >= ?",
            (float(high_threshold),),
        ).fetchone()["n"]
    )
    return {"total": total, "by_status": by_status, "high_confidence": high}
