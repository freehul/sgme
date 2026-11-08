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

from sgme.data import near_dup_dao
from sgme.operations.errors import (
    ERR_INTERNAL,
    ERR_INVALID_ARGS,
    ERR_NOT_FOUND,
    InvalidArgs,
    OperationResult,
)

# 列表分页硬上限（防查询放大，对齐 demand.py 的 MAX_LIMIT 先例）
MAX_LIMIT: int = 200
DEFAULT_LIMIT: int = 50

# 批量拒绝单次条数上限（D2，2026-11-05）：一页 30 条足矣，500 是防滥用的硬闸。
MAX_BATCH_REJECT: int = 500

# auto_resolve_expired 的保守直存标记（去重锚点：notes 中已有该串不重复追加）
AUTO_RESOLVE_MARK: str = "未经人工确认"

DEFAULT_RESOLVE_DAYS: int = 7

# 拒绝理由缺省值（2026-11-05 用户定：理由非必填）
DEFAULT_REJECT_REASON: str = "人工拒绝（未填理由）"


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
    # 2026-11-05 用户定：理由**非必填**（与近似重复复核同口径）。
    # 空理由记默认串，保留「人工拒绝」这一事实本身，便于后续按来源区分。
    reason_val = str(reason).strip() if reason is not None and str(reason).strip() else DEFAULT_REJECT_REASON
    row = conn.execute(
        "SELECT status FROM memories WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"记忆不存在: {memory_id}")

    if row["status"] != "rejected":
        conn.execute(
            "UPDATE memories SET status = 'rejected', reject_reason = ?, rejected_at = ? "
            "WHERE memory_id = ?",
            (reason_val, _now_iso(), memory_id),
        )
        conn.commit()
    return OperationResult.succeed({"memory_id": memory_id})


# ---------- 操作 3b：批量拒绝（D2，2026-11-05） ----------
#
# 背景：收尾交接 D2 登记「WebUI 批量拒绝 = 循环 N 次单条调用」——一页 30 条就是
# 30 次 HTTP + 30 次鉴权，失败还要逐条在前端拼错误文案。本函数把同一页的拒绝
# 收敛成一次调用，语义**完全复用 reject()**（幂等、软删、理由非必填），
# 只多做一件事：单条失败不打断整批，逐条回报结果，让前端能显示「成功 n / 失败 m」。

def batch_reject(
    conn: sqlite3.Connection,
    memory_ids: list[str] | tuple[str, ...] | None,
    reason: str | None = "",
) -> dict:
    """批量拒绝待审记忆：逐条复用 reject()，单条失败不打断整批。

    与单条端点同语义（幂等、软删、理由非必填），差别只在回报形态：
    返回逐条结果而不是一遇到失败就整体 404。

    Args:
        conn: memory.db 连接。
        memory_ids: 记忆 id 列表（顺序即结果顺序；重复 id 不折叠，第二次为幂等 no-op）。
        reason: 拒绝理由（可空；空则每行落 DEFAULT_REJECT_REASON，与单条同口径）。

    Returns:
        {"results": [{"memory_id", "ok", "error"?, "error_code"?}],
         "rejected": n, "failed": m}
        —— failed == len(results) - rejected（含 id 非法与不存在的条目）。

    Raises:
        InvalidArgs: memory_ids 不是列表 / 空列表 / 超过 MAX_BATCH_REJECT。
    """
    if memory_ids is None or isinstance(memory_ids, (str, bytes)):
        raise InvalidArgs("memory_ids 必须是待审记忆 id 的数组")
    if not isinstance(memory_ids, (list, tuple)):
        raise InvalidArgs(f"memory_ids 必须是数组: {type(memory_ids).__name__}")
    if not memory_ids:
        raise InvalidArgs("memory_ids 不能为空数组")
    if len(memory_ids) > MAX_BATCH_REJECT:
        raise InvalidArgs(f"单次批量拒绝不得超过 {MAX_BATCH_REJECT} 条: {len(memory_ids)}")

    reason_val = "" if reason is None else str(reason)
    results: list[dict] = []
    rejected = 0
    for raw in memory_ids:
        # 单条 id 非法只记这一条失败（前端点错/脏数据不该炸掉整批）
        mid = raw.strip() if isinstance(raw, str) else ""
        if not mid:
            results.append({
                "memory_id": "" if raw is None else str(raw),
                "ok": False,
                "error": "memory_id 非法（必须是非空字符串）",
                "error_code": ERR_INVALID_ARGS,
            })
            continue
        try:
            one = reject(conn, mid, reason_val)
        except InvalidArgs as e:
            results.append({
                "memory_id": mid, "ok": False,
                "error": e.message, "error_code": e.error_code,
            })
            continue
        except sqlite3.Error as e:
            # 单行写失败（如短暂锁冲突）不连坐其余条目
            results.append({
                "memory_id": mid, "ok": False,
                "error": f"数据库错误: {e}", "error_code": ERR_INTERNAL,
            })
            continue
        if one.ok:
            rejected += 1
            results.append({"memory_id": mid, "ok": True})
        else:
            results.append({
                "memory_id": mid,
                "ok": False,
                "error": one.message or "拒绝失败",
                "error_code": one.error_code,
            })
    return OperationResult.succeed({
        "results": results,
        "rejected": rejected,
        "failed": len(results) - rejected,
    })


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


# ---------- 操作 5-7：近似重复隔离区人工批复（2026-11-05，B313） ----------
#
# 背景：l3_adjudicate._near_dup_check 把「拿不准」的新记忆挡进
# near_dup_quarantine（灰区），设计上等人工复核。但 2026-11-05 核查发现：
# 整条人工通道**只有 DAO、没有端点/工具/界面**——458 条 pending 从未被处理过，
# 不是没人清，是没有清的手段。用户同日拍板补通道，并要求两侧都能留痕
#（放行已有 restore_reason；丢弃本批补 discard_reason）。
# 用户另定：理由**不设必填**——批量清理要快，但字段留空也要留痕能力。

def list_quarantine(
    conn: sqlite3.Connection,
    status: str | None = "pending",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """近似重复隔离区分页列表（灰区复核唯一数据源）。

    items 每条带 matched_content——被撞上的既有记忆原文。灰区复核必须
    「左右并排看」才能判断是不是同一件事，故 DAO 侧 LEFT JOIN 一次带出
    （不做 N+1 查询）。

    Args:
        conn: memory.db 连接。
        status: pending / discarded / restored；None 或空串 = 不过滤。
        limit: 页大小（1-200）。
        offset: 跳过条数（≥0）。

    Raises:
        InvalidArgs: limit/offset 非法。
    """
    limit_val = _parse_limit(limit)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidArgs(f"offset 必须 ≥ 0: {offset!r}")
    st = (status or "").strip() or None
    items = near_dup_dao.list_items_with_matched(
        conn, status=st, limit=limit_val, offset=offset
    )
    total = near_dup_dao.count_items(conn, status=st)
    return OperationResult.succeed({"items": items, "total": total})


def _parse_quarantine_id(id: object) -> int:
    """隔离行 id 校验：必须是正整数（HTTP 路径参数为 str，MCP 为 int）。"""
    try:
        qid = int(id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise InvalidArgs(f"id 必须是整数: {id!r}")
    if qid < 1:
        raise InvalidArgs(f"id 必须 ≥ 1: {qid}")
    return qid


def discard_quarantine(
    conn: sqlite3.Connection,
    id: object,
    reason: str | None = None,
) -> dict:
    """丢弃一条近似重复：置 discarded，不写进记忆池。

    状态机：仅 pending 可丢弃（DAO 内 WHERE status='pending' 兜底）；已丢弃 /
    已补录返回 ok=False 并回带当前状态，不报错——重复点击是正常操作。

    reason 非必填（用户 2026-11-05 拍板），非空时落 discard_reason 留痕。
    """
    qid = _parse_quarantine_id(id)
    item = near_dup_dao.get_item(conn, qid)
    if item is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"隔离行不存在: id={qid}")
    reason_val = str(reason).strip() if reason is not None and str(reason).strip() else None
    ok = near_dup_dao.mark_discarded(conn, qid, reason=reason_val)
    return OperationResult.succeed({
        "id": qid,
        "ok": bool(ok),
        "status": "discarded" if ok else item.get("status"),
        "reason": reason_val,
    })


def bulk_discard_quarantine(
    conn: sqlite3.Connection,
    min_score: float = 0.92,
    reason: str | None = None,
    apply: bool = False,
) -> dict:
    """批量丢弃 pending 近似重复（2026-11-08 用户拍板：≥0.92 可直接丢）。

    apply=False 只统计（dry-run）；apply=True 执行 UPDATE，行保留可审计/可 restore。
    """
    try:
        score = float(min_score)
    except (TypeError, ValueError):
        raise InvalidArgs(f"min_score 必须是数字: {min_score!r}")
    if not 0.0 <= score <= 1.0:
        raise InvalidArgs(f"min_score 必须在 [0,1]: {score}")
    reason_val = str(reason).strip() if reason is not None and str(reason).strip() else None
    matched = near_dup_dao.count_pending_ge(conn, score)
    if not apply:
        return OperationResult.succeed({
            "dry_run": True, "min_score": score, "matched": matched, "discarded": 0,
        })
    discarded = near_dup_dao.bulk_discard(conn, min_score=score, reason=reason_val)
    return OperationResult.succeed({
        "dry_run": False, "min_score": score, "matched": matched,
        "discarded": discarded, "reason": reason_val,
    })


def restore_quarantine(
    conn: sqlite3.Connection,
    id: object,
    reason: str | None = None,
    cfg: dict | None = None,
    client=None,
    supersede: bool = True,
) -> dict:
    """放行一条近似重复：补录进记忆池 + 向量即时回填。

    复用 near_dup_dao.restore（全字段写入 + TTL 回填 + 向量即时回填）。
    已补录过的行抛 ValueError → 转 ERR_INVALID_ARGS（不重复补录）。

    supersede（默认 True，用户 2026-11-05 定）：人工放行 = 判定新内容更准，故
    新记忆用人工确认档 priority=80，并把被撞上的旧记忆 mark_superseded（退出
    注入/召回，不删可溯源）。传 False 则两条并存——适用于「都是独立事实、
    只是措辞相似」的情况。
    """
    qid = _parse_quarantine_id(id)
    reason_val = str(reason).strip() if reason is not None and str(reason).strip() else "人工补录"
    try:
        new_id = near_dup_dao.restore(
            conn, qid, reason_val, cfg or {}, client=client, supersede=bool(supersede),
        )
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    return OperationResult.succeed({
        "id": qid,
        "memory_id": new_id,
        "status": "restored",
        "reason": reason_val,
        "supersede": bool(supersede),
    })
