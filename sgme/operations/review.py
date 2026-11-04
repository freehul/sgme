"""operations/review.py：待审记忆队列操作层（设计决策 D-30，SGME 2.0）。

L2 提炼（D-23 二轮修订 P1-2）会对低置信度产物打 needs_review=1 的待审闸门：
不进主注入块、不进人格证据，只在本队列可见，等待人工放行（approve）或
拒绝（reject）；超期（默认 7 天）未处理的由 auto_resolve_expired 按
**保守直存**消化——放行进库但在 notes 追加「未经人工确认」标记（去重），
保证记忆池可溯源。

分层（v0.7 §7）：本模块不认识协议——不 import fastapi/mcp、不知道 HTTP 状态码；
SQL 直接作用于 memories / memory_tags（不建新表，2.0 列由 data/db.py 迁移负责），
入口层（HTTP/MCP）只做协议翻译。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs, OperationResult

# 列表分页硬上限（防查询放大，对齐 demand.py 的 MAX_LIMIT 先例）
MAX_LIMIT: int = 200
DEFAULT_LIMIT: int = 50

# auto_resolve_expired 的保守直存标记（去重锚点：notes 中已有该串不重复追加）
AUTO_RESOLVE_MARK: str = "未经人工确认"

DEFAULT_RESOLVE_DAYS: int = 7


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（与 data 层同格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_days_ago(days: int) -> str:
    """days 天前的 UTC ISO 时间戳（库内定宽格式可比：字典序 == 时间序）。"""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _parse_limit(raw: int, name: str = "limit") -> int:
    """分页参数：≥1 且 ≤ MAX_LIMIT。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidArgs(f"{name} 必须是整数: {raw!r}")
    if raw < 1:
        raise InvalidArgs(f"{name} 必须 ≥ 1: {raw}")
    if raw > MAX_LIMIT:
        raise InvalidArgs(f"{name} 不得超过 {MAX_LIMIT}: {raw}")
    return raw


def _append_note(current: str | None, text: str) -> str:
    """notes 追加式拼接：已有内容时换行衔接，绝不覆盖既有备注（对齐 idea_dao 铁律）。"""
    if current is not None and str(current).strip():
        return f"{current}\n{text}"
    return text


# ---------- 操作 1：待审列表 ----------

def list_pending(
    conn: sqlite3.Connection,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    dimension: str | None = None,
) -> dict:
    """待审队列分页列表：needs_review=1 且 status='active'，按 created_at 倒序。

    Args:
        conn: memory.db 连接。
        limit: 页大小（1-200）。
        offset: 跳过条数（≥0）。
        dimension: 维度过滤——走 memory_tags 表关联（精确匹配 dimension_id）。

    Returns:
        ``{"items": [...], "total": int}``——items 为完整记忆行字典，
        total 为过滤条件下的总数（不受分页影响）。

    Raises:
        InvalidArgs: limit/offset 非法。
    """
    limit_val = _parse_limit(limit)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidArgs(f"offset 必须 ≥ 0: {offset!r}")

    # 维度过滤：EXISTS 半连接（不产生重复行，与逐条打多标签的写入形态匹配）
    dim_clause = ""
    params: list = []
    if dimension is not None and str(dimension).strip():
        dim_clause = (
            "AND EXISTS (SELECT 1 FROM memory_tags mt "
            "WHERE mt.memory_id = m.memory_id AND mt.dimension_id = ?)"
        )
        params.append(str(dimension).strip())

    where = f"WHERE m.needs_review = 1 AND m.status = 'active' {dim_clause}"

    total = conn.execute(
        f"SELECT COUNT(*) FROM memories m {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT m.* FROM memories m {where} "
        f"ORDER BY m.created_at DESC, m.memory_id LIMIT ? OFFSET ?",
        (*params, limit_val, offset),
    ).fetchall()
    items = [dict(r) for r in rows]
    # 2026-11-04（B292）：待审列表附带 dimensions——此前只有 SELECT m.*，
    # WebUI 只能逐条 GET /v1/memory/{id} 补维度（N+1）；一次批量查 memory_tags 补齐。
    if items:
        ids = [it["memory_id"] for it in items]
        marks = ",".join("?" * len(ids))
        tag_rows = conn.execute(
            "SELECT memory_id, dimension_id FROM memory_tags "
            f"WHERE memory_id IN ({marks}) ORDER BY dimension_id",
            tuple(ids),
        ).fetchall()
        by_mem: dict[str, list[str]] = {}
        for tr in tag_rows:
            by_mem.setdefault(tr["memory_id"], []).append(tr["dimension_id"])
        for it in items:
            it["dimensions"] = by_mem.get(it["memory_id"], [])
    return OperationResult.succeed({"items": items, "total": total})


# ---------- 操作 2：人工放行 ----------

def approve(conn: sqlite3.Connection, memory_id: str, note: str | None = None) -> dict:
    """放行待审记忆：置 needs_review=0，记忆回到正常注入/检索可见状态。

    幂等：已是放行态（needs_review=0）时为 no-op，不重复写备注。

    Args:
        conn: memory.db 连接。
        memory_id: 记忆 id。
        note: 人工放行备注（非空时追加进 notes 列，带时间戳可溯源）。

    Returns:
        ``{"ok": bool, "memory_id": str}``；不存在时 ``{"ok": False, "error": "not_found"}``。
    """
    row = conn.execute(
        "SELECT needs_review, notes FROM memories WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"记忆不存在: {memory_id}")

    if row["needs_review"] == 1:
        conn.execute(
            "UPDATE memories SET needs_review = 0 WHERE memory_id = ?", (memory_id,)
        )
        if note is not None and str(note).strip():
            stamped = f"[{_now_iso()}] 放行：{str(note).strip()}"
            conn.execute(
                "UPDATE memories SET notes = ? WHERE memory_id = ?",
                (_append_note(row["notes"], stamped), memory_id),
            )
        conn.commit()
    return OperationResult.succeed({"memory_id": memory_id})


# ---------- 操作 3：人工拒绝 ----------

def reject(conn: sqlite3.Connection, memory_id: str, reason: str) -> dict:
    """拒绝待审记忆：置 status='rejected' + reject_reason + rejected_at（软删，可溯源可恢复）。

    幂等：已是拒绝态时为 no-op，不覆盖首次拒绝理由与时刻。

    Args:
        conn: memory.db 连接。
        memory_id: 记忆 id。
        reason: 拒绝理由（人可读，落 reject_reason 列）。

    Returns:
        ``{"ok": bool, "memory_id": str}``；不存在时 ``{"ok": False, "error": "not_found"}``。

    Raises:
        InvalidArgs: reason 为空。
    """
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidArgs("reason 不能为空")
    row = conn.execute(
        "SELECT status FROM memories WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"记忆不存在: {memory_id}")

    if row["status"] != "rejected":
        conn.execute(
            "UPDATE memories SET status = 'rejected', reject_reason = ?, rejected_at = ? "
            "WHERE memory_id = ?",
            (reason.strip(), _now_iso(), memory_id),
        )
        conn.commit()
    return OperationResult.succeed({"memory_id": memory_id})


# ---------- 操作 4：超期保守直存 ----------

def auto_resolve_expired(
    conn: sqlite3.Connection, days: int = DEFAULT_RESOLVE_DAYS
) -> dict:
    """消化超期未处理的待审记忆（7 天无人工动作 → 保守直存）。

    策略（D-30）：不丢弃——放行进库（needs_review=0）但**追加「未经人工确认」
    标记到 notes**（去重：已有该标记不重复追加），保证记忆池可溯源、后续可
    按标记批量复查。created_at 早于 days 天前的待审记忆（needs_review=1，
    不限 status——超期消化也应覆盖已被人工拒绝之外的异常态）视为超期。

    Args:
        conn: memory.db 连接。
        days: 超期天数阈值，默认 7。

    Returns:
        ``{"resolved": int}``——本次实际消化的记忆条数。

    Raises:
        InvalidArgs: days < 1。
    """
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise InvalidArgs(f"days 必须 ≥ 1: {days!r}")

    cutoff = _iso_days_ago(days)
    rows = conn.execute(
        "SELECT memory_id, notes FROM memories "
        "WHERE needs_review = 1 AND created_at < ?",
        (cutoff,),
    ).fetchall()

    resolved = 0
    for r in rows:
        notes = r["notes"]
        already_marked = notes is not None and AUTO_RESOLVE_MARK in str(notes)
        if already_marked:
            new_notes = notes
        else:
            stamped = f"[{_now_iso()}] {AUTO_RESOLVE_MARK}（超期 {days} 天保守直存）"
            new_notes = _append_note(notes, stamped)
        conn.execute(
            "UPDATE memories SET needs_review = 0, notes = ? WHERE memory_id = ?",
            (new_notes, r["memory_id"]),
        )
        resolved += 1
    conn.commit()
    return OperationResult.succeed({"resolved": resolved})
