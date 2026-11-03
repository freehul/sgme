"""engine/ttl_disposition.py：D-52 TTL 到期五档处置（L4 梦境任务，零 LLM）。

设计依据：SGME2.0-设计方案-V1.0 §3.3.1。两个正交闸门 + 五档顺序：

    0  闸门 A（断言有效期）：modality IN ('planned','wished') 且 valid_to 已过
       → 退出注入（inject_tier='none'），lifecycle 不动，**不进 1–4 档**
    0″ 计划未到期豁免：modality IN ('planned','wished') 且 valid_to > now → 不处置
    0′ 闸门 B 未到期（或 ttl_days IS NULL）→ 不处置、不写审计行
    1  到期 + M ≥ 高档分位 + ttl_renew_count < renew_max
       → 续期：ttl_renewed_at = 旧到期日、count+1、lifecycle=active
    2  到期 + M ≥ 高档分位 + 续期已满 → inject_tier='anchor'（锚点块）
    3  到期 + 低档分位 ≤ M < 高档分位   → inject_tier='anchor'
    4  到期 + M < 低档分位              → lifecycle=sleeping（sleep_days 后转 archived）

关键纪律：
- **2.0 起不再写 status='expired'**（D-46 三轴分离）：status 只表达质量，到期只写
  lifecycle / inject_tier / ttl_disposition 三处。旧行为保留在 dream._mark_expired_ttl，
  由 config ttl.disposition.enabled=false 回退。
- 存量首轮防「续期跑步机」（§3.3.1 三轮修订 P2-1）：旧到期日超出
  legacy_renew_periods 个 TTL 周期的存量直接按 M 定档（2/3/4），不走续期档。
- 阈值用**分位**（9.11.4：绝对阈值不适用于新 M）。
- 审计只在**产生处置**时写一行（未到期不记账）。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from sgme.engine import meaning_anchor
from sgme.engine.recall_aging import compute_ttl_multiplier, record_disposition

logger = logging.getLogger("sgme.engine.ttl_disposition")

DEFAULT_DISPOSITION_CFG = {
    "enabled": True,
    "renew_max": 2,
    "tier_high_ratio": 0.67,
    "tier_low_ratio": 0.33,
    "sleep_days": 30,
    "legacy_renew_periods": 1,
}


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _add_days(base: datetime, days: float) -> datetime:
    return base + timedelta(days=float(days))


def _disposition_cfg(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_DISPOSITION_CFG)
    ttl_cfg = (cfg or {}).get("ttl") or {}
    usr = ttl_cfg.get("disposition") or {}
    if isinstance(usr, dict):
        for k, v in usr.items():
            if k in merged and v is not None:
                merged[k] = v
    return merged


def _fetch_gate_a(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """闸门 A：计划/愿望的断言有效期已过（尚未处置过的）。"""
    return conn.execute(
        """
        SELECT memory_id, valid_to, meaning_anchor
        FROM memories
        WHERE status='active'
          AND modality IN ('planned','wished')
          AND valid_to IS NOT NULL AND valid_to != ''
          AND julianday(valid_to) < julianday('now')
          AND COALESCE(inject_tier, '') != 'none'
        """
    ).fetchall()


def _fetch_gate_b(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """闸门 B 候选（粗筛：只取有 TTL、生命周期未终态的；精判在 Python 侧算生效 TTL）。"""
    return conn.execute(
        """
        SELECT m.memory_id, m.ttl_days, m.ttl_renew_count, m.ttl_renewed_at,
               m.updated_at, m.modality, m.meaning_anchor, m.memory_type,
               COALESCE(m.inject_tier, 'full') AS inject_tier,
               COALESCE(s.recall_count, 0) AS recall_count
        FROM memories m
        LEFT JOIN memory_stats s ON s.memory_id = m.memory_id
        WHERE m.status='active'
          AND m.ttl_days IS NOT NULL
          AND COALESCE(m.lifecycle, 'active') = 'active'
        """
    ).fetchall()


def dispose(conn: sqlite3.Connection, cfg: dict | None = None, *, now: datetime | None = None) -> dict:
    """执行一次五档处置，返回各类处置计数。

    Args:
        conn: memory.db 连接。
        cfg: 运行时配置（读 ttl.disposition）。
        now: 注入时钟（测试用）。
    """
    now = now or datetime.now(UTC)
    dcfg = _disposition_cfg(cfg)
    high = float(dcfg["tier_high_ratio"])
    low = float(dcfg["tier_low_ratio"])
    renew_max = int(dcfg["renew_max"])
    legacy_periods = float(dcfg["legacy_renew_periods"])

    # 阈值：分位现算（不足 3 条有锚记忆时退回等权中性 0.5 / 0.35，避免全表误判）
    hi_th, lo_th = meaning_anchor.tier_thresholds(conn, high_ratio=high, low_ratio=low)
    if hi_th is None or lo_th is None:
        hi_th, lo_th = 0.50, 0.35
        logger.info("meaning_anchor 覆盖不足，TTL 分档退回中性阈值 %s / %s", hi_th, lo_th)

    stats = {"valid_expired": 0, "renewed": 0, "anchor": 0, "sleeping": 0, "skipped_no_anchor": 0}
    now_iso = _now_iso(now)

    # ---- 0 闸门 A：失效计划/愿望 → 退出注入，不进 1–4 档 ----
    for row in _fetch_gate_a(conn):
        mid = str(row["memory_id"])
        conn.execute(
            "UPDATE memories SET inject_tier='none' WHERE memory_id=?", (mid,)
        )
        record_disposition(
            conn, mid, "valid_expired",
            old_expire_at=str(row["valid_to"]), new_expire_at=None,
            m_score=float(row["meaning_anchor"] or 0.0), renew_count=0,
        )
        stats["valid_expired"] += 1
    conn.commit()

    # ---- 0′/0″/1–4 闸门 B ----
    for row in _fetch_gate_b(conn):
        mid = str(row["memory_id"])
        modality = str(row["modality"] or "unknown").lower()
        ttl_days = float(row["ttl_days"])
        renew_count = int(row["ttl_renew_count"] or 0)
        inject_tier = str(row["inject_tier"] or "full")
        base_ts = row["ttl_renewed_at"] or row["updated_at"]
        base_dt = _parse_iso(base_ts)
        if base_dt is None or ttl_days <= 0:
            continue

        # 0″：计划/愿望的命运只由闸门 A 管（未到期不因维度 TTL 掉档）
        if modality in ("planned", "wished"):
            continue

        # D-73 加固：有效性放大与续期共享 3× 上限（用户 10-30 拍板）
        mult = 1.0 if str(row["memory_type"] or "") == "forget_request" else compute_ttl_multiplier(
            int(row["recall_count"] or 0)
        )
        effective_ttl = ttl_days * min(mult, 1.0 + renew_count, 3.0)
        expire_dt = _add_days(base_dt, effective_ttl)
        if expire_dt > now:
            continue  # 0′ 未到期：不处置、不写审计

        m_value = row["meaning_anchor"]
        if m_value is None:
            stats["skipped_no_anchor"] += 1
            continue  # 与设计一致：无 M 不猜分，等下次梦境回填后判定

        m_score = float(m_value)
        old_expire_iso = expire_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # 存量首轮防跑步机：旧到期日早于 legacy_periods 个 TTL 周期前 → 跳过续期档
        stale = (now - expire_dt).total_seconds() > legacy_periods * ttl_days * 86400.0
        already_anchor = inject_tier == "anchor"

        if m_score >= hi_th and renew_count < renew_max and not stale:
            new_expire = _add_days(expire_dt, effective_ttl)
            conn.execute(
                "UPDATE memories SET ttl_renewed_at=?, ttl_renew_count=?, "
                "lifecycle='active', inject_tier='full' WHERE memory_id=?",
                (old_expire_iso, renew_count + 1, mid),
            )
            record_disposition(
                conn, mid, "renewed",
                old_expire_at=old_expire_iso,
                new_expire_at=new_expire.strftime("%Y-%m-%dT%H:%M:%SZ"),
                m_score=m_score, renew_count=renew_count + 1,
            )
            stats["renewed"] += 1
        elif m_score >= lo_th:
            # 档 2（高 M 续期满/存量）与档 3（中 M）同处置：长期锚点块
            if already_anchor:
                continue  # 已在锚点块且仍在中高档：无状态变化，不重复记账
            conn.execute(
                "UPDATE memories SET inject_tier='anchor', lifecycle='active' WHERE memory_id=?",
                (mid,),
            )
            record_disposition(
                conn, mid, "anchor",
                old_expire_at=old_expire_iso, new_expire_at=None,
                m_score=m_score, renew_count=renew_count,
            )
            stats["anchor"] += 1
        else:
            conn.execute(
                "UPDATE memories SET lifecycle='sleeping', inject_tier='none' WHERE memory_id=?",
                (mid,),
            )
            record_disposition(
                conn, mid, "sleeping",
                old_expire_at=old_expire_iso, new_expire_at=None,
                m_score=m_score, renew_count=renew_count,
            )
            stats["sleeping"] += 1
    conn.commit()
    logger.info("TTL 五档处置完成: %s", stats)
    return stats


def archive_sleeping(conn: sqlite3.Connection, sleep_days: int = 30, *, now: datetime | None = None) -> int:
    """休眠满 sleep_days 天的记忆转 archived（D-52 档 4 收尾）。返回归档条数。

    判据用 ttl_disposition 里**最近一条 sleeping 审计行**的时间——lifecycle 本身不带
    时间戳，避免为此再加一列（D-52 的数据模型只要求四列）。
    """
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=int(sleep_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT m.memory_id, d.last_ts
        FROM memories m
        JOIN (
            SELECT memory_id, MAX(created_at) AS last_ts
            FROM ttl_disposition
            WHERE disposition='sleeping'
            GROUP BY memory_id
        ) d ON d.memory_id = m.memory_id
        WHERE m.lifecycle='sleeping' AND d.last_ts <= ?
        """,
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0
    for row in rows:
        mid = str(row["memory_id"])
        conn.execute(
            "UPDATE memories SET lifecycle='archived', inject_tier='none' WHERE memory_id=?",
            (mid,),
        )
        record_disposition(
            conn, mid, "archived",
            old_expire_at=None, new_expire_at=None,
            m_score=0.0, renew_count=0,
        )
    conn.commit()
    logger.info("休眠转归档: %d 条", len(rows))
    return len(rows)
