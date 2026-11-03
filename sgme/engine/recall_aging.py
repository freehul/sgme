"""召回加固：幂律有效期放大 + TTL 处置审计（D-73，2026-11-01）。

设计决策 D-73（`SGME2.0-设计方案-V1.0.md`）：记忆被检索/注入命中越多
（recall_count 越大），有效期按幂律放大——热度即生命力，常被消费的记忆
不应机械地按同一 TTL 过期出池；上限 3×，防「滚雪球式永生」。

- ``compute_ttl_multiplier``：纯计算。系数 = 1 + log2(1+recall_count)*0.5，
  截断到 cap（默认 3.0）；单调递增、有上限；recall_count<=0 归一（1.0）。
- ``apply_recall_aging``：读 memory_stats.recall_count 与 memories.memory_type，
  返回放大后的 ttl_days。``forget_request`` 类型**豁免**（永不放大）——
  「用户要求遗忘」的清理指令若被「记得要去删」这个动作本身续命，
  被遗忘的信息就会赖在池里不走，违背遗忘语义。
- ``ensure_ttl_disposition_table`` / ``record_disposition``：审计表
  ttl_disposition 逐次记录 TTL 处置（过期判定/续期/锚定/休眠/归档），
  供 Dream 日报与运维排查回溯「每条记忆为何活/为何死」。

边界：本模块只计算与审计，**不写 memories 表**——放大结果如何落库、
何时与 dream._mark_expired_ttl 联动，由接线方负责（职责分离）。
"""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import UTC, datetime

logger = logging.getLogger("sgme.engine.recall_aging")

# forget_request 豁免判定依据的 memory_type 值（memories.memory_type）
_FORGET_REQUEST_TYPE = "forget_request"

# ttl_disposition 合法处置集（契约固定五值；大小写敏感，不静默纠错）
VALID_DISPOSITIONS = frozenset({"valid_expired", "renewed", "anchor", "sleeping", "archived"})

# 审计表 DDL（IF NOT EXISTS 幂等；照 db.py 独立 DDL 常量先例，不进 MEMORY_DDL）
TTL_DISPOSITION_DDL = """
CREATE TABLE IF NOT EXISTS ttl_disposition(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT,
  disposition TEXT,
  m_score REAL,
  renew_count INTEGER,
  old_expire_at TEXT,
  new_expire_at TEXT,
  created_at TEXT
);
"""


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（对齐 memory_stats_dao._now_iso 口径）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_ttl_multiplier(recall_count: int, cap: float = 3.0) -> float:
    """幂律放大系数：命中越多放大越大，封顶 cap。

    公式：``1 + min(cap - 1, log2(1 + recall_count) * 0.5)``。
    锚点：n=0→1.0、n=1→1.5、n=3→2.0、n=7→2.5、n≥15→3.0（默认 cap）。

    Args:
        recall_count: 累计命中次数（memory_stats.recall_count；<=0 视为未命中）。
        cap: 放大上限（默认 3.0）；cap=1.0 时恒为 1.0（min 项归零）。

    Returns:
        float 放大系数，范围 [1.0, cap]，单调递增。
    """
    if recall_count <= 0:
        return 1.0
    grow = math.log2(1 + recall_count) * 0.5
    return 1.0 + min(cap - 1.0, grow)


def apply_recall_aging(conn: sqlite3.Connection, memory_id: str, base_ttl_days: int) -> dict:
    """按召回热度计算放大后的有效期（只计算，不落库）。

    Args:
        conn: memory.db 连接（memories 与 memory_stats 同库）。
        memory_id: 记忆 id。
        base_ttl_days: 基础有效期（天；通常取 memories.ttl_days 当前值）。

    Returns:
        普通记忆：``{"ttl_days": int, "multiplier": float, "exempt": False}``；
        forget_request：``{"ttl_days": base_ttl_days, "multiplier": 1.0,
        "exempt": True, "reason": "forget_request"}``。
        memories 无此行或从未被命中（无 memory_stats 行）→ 倍率 1.0 原样返回。
    """
    # memory_type 决定豁免与否（memories 无此行 → memory_type 为 None → 不豁免）
    row = conn.execute(
        "SELECT memory_type FROM memories WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    memory_type = row["memory_type"] if row is not None else None

    if memory_type == _FORGET_REQUEST_TYPE:
        logger.debug("recall_aging 豁免：memory_id=%s (forget_request)", memory_id)
        return {"ttl_days": base_ttl_days, "multiplier": 1.0, "exempt": True,
                "reason": "forget_request"}

    stats = conn.execute(
        "SELECT recall_count FROM memory_stats WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    recall_count = int(stats["recall_count"] or 0) if stats is not None else 0

    multiplier = compute_ttl_multiplier(recall_count)
    return {
        "ttl_days": int(base_ttl_days * multiplier),  # int 截断：宁短勿长
        "multiplier": multiplier,
        "exempt": False,
    }


def ensure_ttl_disposition_table(conn: sqlite3.Connection) -> None:
    """建 ttl_disposition 审计表（CREATE TABLE IF NOT EXISTS，重复建无害）。"""
    conn.executescript(TTL_DISPOSITION_DDL)
    conn.commit()


def record_disposition(
    conn: sqlite3.Connection,
    memory_id: str,
    disposition: str,
    old_expire_at: str | None,
    new_expire_at: str | None,
    m_score: float = 0.0,
    renew_count: int = 0,
    reason: str | None = None,
) -> None:
    """写一条 TTL 处置审计（谁因何被判定为何种去向，逐次留痕）。

    Args:
        conn: memory.db 连接。
        memory_id: 处置的记忆 id。
        disposition: 处置类型，必须是 VALID_DISPOSITIONS 之一
            （valid_expired/renewed/anchor/sleeping/archived），其他值抛 ValueError。
        old_expire_at: 处置前过期时刻（ISO；可 None）。
        new_expire_at: 处置后过期时刻（ISO；可 None）。
        m_score: 记忆活跃度分（M 分；缺省 0.0）。
        renew_count: 累计续期次数（缺省 0）。
        reason: 处置来源（D-73 召回加固记 'recall_reinforced'；D-52 五档留空）。

    Raises:
        ValueError: disposition 不在合法集合内（含大小写不符）。
    """
    if disposition not in VALID_DISPOSITIONS:
        raise ValueError(
            f"非法 disposition: {disposition!r}（合法值：{sorted(VALID_DISPOSITIONS)}）"
        )
    # 写前确保表就绪（幂等；调用方漏调 ensure 时兜底，避免审计静默丢失）
    ensure_ttl_disposition_table(conn)
    conn.execute(
        """
        INSERT INTO ttl_disposition
          (memory_id, disposition, reason, m_score, renew_count,
           old_expire_at, new_expire_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (memory_id, disposition, reason, float(m_score), int(renew_count),
         old_expire_at, new_expire_at, _now_iso()),
    )
    conn.commit()
