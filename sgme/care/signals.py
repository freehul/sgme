# -*- coding: utf-8 -*-
"""sgme/care/signals.py：关怀信号扫描器（T-36，ST-25）。

SGME 侧信号增强：从记忆池**推导**关怀信号（零 LLM 规则引擎），写入
signal_events（type=care_*，source='care'）。消费方（agent）拉取未消费信号，
决定是否打扰用户——SGME 只发信号不做决策（架构铁律）。

与 Dream 协同：定时扫描而非常驻轮询（首次手动触发时拉起定时器，Dream 同款）。

信号类型（2026-08-13 定义）：
- ``care_todo_due``：待办到期/无进展（tasks 维度，updated_at 老化 ≥ todo_due_days 天）
- ``care_mood``：情绪低落（status 维度，关键词匹配：低落/疲惫/压力/焦虑/难受/累）
- ``care_overwork``：过劳预警（focus 维度，当日新增 ≥ overwork_threshold 条）
- ``care_daily``：每日关怀（定时信号，供消费方早安/晚安问候，dedup_key=日期）

幂等：事件 id = uuid5(命名空间, "{type}:{dedup_key}")，INSERT OR IGNORE 天然去重——
同一信号重复扫描不会产生重复事件。

默认配置（config care 段）：
- todo_due_days: 7        # tasks 记忆无进展多少天触发
- overwork_threshold: 5   # 当日 focus 记忆新增多少条触发
- mood_keywords: [...]    # status 维度情绪关键词
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sgme.data import signal_dao

logger = logging.getLogger("sgme.care.signals")

# uuid5 命名空间（care 信号确定性 id）
_CARE_NS = uuid.UUID("6f1e3a5c-9c4a-4e3b-8f2d-1a0b9c8d7e6f")

# 信号类型常量
CARE_TODO_DUE = "care_todo_due"
CARE_MOOD = "care_mood"
CARE_OVERWORK = "care_overwork"
CARE_DAILY = "care_daily"
CARE_TYPES = (CARE_TODO_DUE, CARE_MOOD, CARE_OVERWORK, CARE_DAILY)

# 默认情绪关键词（status 维度内容匹配，零 LLM）
DEFAULT_MOOD_KEYWORDS = ("低落", "疲惫", "压力", "焦虑", "难受", "很累", "崩溃", "烦躁")

# 技术语境排除词（2026-08-13 真实链路发现：'崩溃' 误命中技术记忆
# 「test_dream 崩溃确认为偶发竞态」——bug/测试语境不是情绪低落）
# ⚠️ 边界（2026-08-13 端到端验证）：排除词表会误杀真情绪记忆——
# 内容含"测试"（如「测试：今天心情低落」）会被当技术语境。因此排除词
# 只保留**强技术信号词**（bug/pytest/竞态等），去掉弱词（测试/修复/错误/日志）。
TECHNICAL_CONTEXT_KEYWORDS = (
    "bug", "pytest", "竞态", "偶发", "passed", "failed",
    "崩溃确认", "排查", "单测", "调试", "报错堆栈", "代码仓库",
)

# 推导规则默认值（冷启动默认，用户可在 config care 段覆盖）
DEFAULT_RULES = {
    "todo_due_days": 7,
    "overwork_threshold": 5,
    "mood_keywords": list(DEFAULT_MOOD_KEYWORDS),
}

_DATE_FMT = "%Y-%m-%d"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime(_DATE_FMT)


def _event_id(signal_type: str, dedup_key: str) -> str:
    """确定性事件 id（同类型同 dedup_key 幂等去重）。"""
    return str(uuid.uuid5(_CARE_NS, f"{signal_type}:{dedup_key}"))


def _insert_care_event(mem_conn: sqlite3.Connection, signal_type: str,
                       dedup_key: str, payload: dict[str, Any]) -> bool:
    """写入关怀信号事件（INSERT OR IGNORE 幂等）。

    Returns:
        True=新事件写入；False=已存在（去重跳过）。
    """
    eid = _event_id(signal_type, dedup_key)
    cur = mem_conn.execute(
        "INSERT OR IGNORE INTO signal_events (event_id, type, source, payload, ts, consumed_at)"
        " VALUES (?,?,?,?,?,NULL)",
        (eid, signal_type, "care", json.dumps(payload, ensure_ascii=False), _now_iso()),
    )
    mem_conn.commit()
    if cur.rowcount:
        logger.info("关怀信号: %s dedup_key=%s", signal_type, dedup_key)
        return True
    return False


# ---------- 各类型推导规则（零 LLM） ----------

def _scan_todo_due(mem_conn: sqlite3.Connection, rules: dict[str, Any]) -> int:
    """待办到期/无进展：tasks 维度 active 记忆，updated_at 老化 ≥ todo_due_days 天。"""
    days = int(rules.get("todo_due_days", 7))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = mem_conn.execute(
        """
        SELECT m.memory_id, m.content, m.updated_at FROM memories m
        JOIN memory_tags t ON t.memory_id = m.memory_id
        WHERE t.dimension_id = 'tasks' AND m.status = 'active'
          AND m.updated_at < ?
        ORDER BY m.updated_at ASC LIMIT 20
        """,
        (cutoff,),
    ).fetchall()
    n = 0
    for r in rows:
        dedup_key = r["memory_id"]
        payload = {
            "memory_id": r["memory_id"],
            "content": r["content"][:200],
            "updated_at": r["updated_at"],
            "stale_days": days,
        }
        if _insert_care_event(mem_conn, CARE_TODO_DUE, dedup_key, payload):
            n += 1
    return n


def _scan_mood(mem_conn: sqlite3.Connection, rules: dict[str, Any]) -> int:
    """情绪低落：status 维度 active 记忆，内容命中情绪关键词。

    假阳性防护：内容同时命中技术语境词（bug/测试/代码等）→ 跳过
    （2026-08-13 真实链路：'崩溃' 误命中技术记忆「崩溃确认为偶发竞态」）。
    """
    keywords = rules.get("mood_keywords") or list(DEFAULT_MOOD_KEYWORDS)
    rows = mem_conn.execute(
        """
        SELECT m.memory_id, m.content, m.updated_at FROM memories m
        JOIN memory_tags t ON t.memory_id = m.memory_id
        WHERE t.dimension_id = 'status' AND m.status = 'active'
        ORDER BY m.updated_at DESC LIMIT 50
        """,
    ).fetchall()
    n = 0
    for r in rows:
        content = r["content"] or ""
        hit = [k for k in keywords if k in content]
        if not hit:
            continue
        # 技术语境排除（bug/测试语境中的"崩溃/异常"不是情绪）
        if any(tk in content for tk in TECHNICAL_CONTEXT_KEYWORDS):
            continue
        dedup_key = r["memory_id"]
        payload = {
            "memory_id": r["memory_id"],
            "content": content[:200],
            "updated_at": r["updated_at"],
            "mood_keywords": hit,
        }
        if _insert_care_event(mem_conn, CARE_MOOD, dedup_key, payload):
            n += 1
    return n


def _scan_overwork(mem_conn: sqlite3.Connection, rules: dict[str, Any]) -> int:
    """过劳预警：focus 维度当日新增记忆 ≥ overwork_threshold 条。"""
    threshold = int(rules.get("overwork_threshold", 5))
    today = _today()
    cnt = mem_conn.execute(
        """
        SELECT COUNT(*) AS c FROM memories m
        JOIN memory_tags t ON t.memory_id = m.memory_id
        WHERE t.dimension_id = 'focus' AND m.status = 'active'
          AND substr(m.created_at, 1, 10) = ?
        """,
        (today,),
    ).fetchone()["c"]
    if cnt < threshold:
        return 0
    payload = {"date": today, "focus_count": cnt, "threshold": threshold}
    return 1 if _insert_care_event(mem_conn, CARE_OVERWORK, today, payload) else 0


def _scan_daily(mem_conn: sqlite3.Connection, rules: dict[str, Any]) -> int:
    """每日关怀信号（供早安/晚安问候；同日去重）。"""
    today = _today()
    payload = {"date": today, "note": "每日关怀问候信号"}
    return 1 if _insert_care_event(mem_conn, CARE_DAILY, today, payload) else 0


# ---------- 扫描编排 ----------

def scan_care_signals(mem_conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, int]:
    """执行一次关怀信号扫描（四类推导，幂等去重）。

    Returns:
        {type: 新增条数} 统计。
    """
    care_cfg = (cfg.get("care") or {})
    rules = dict(DEFAULT_RULES)
    rules.update({k: v for k, v in care_cfg.items() if k in DEFAULT_RULES and v is not None})

    stats: dict[str, int] = {}
    stats[CARE_TODO_DUE] = _scan_todo_due(mem_conn, rules)
    stats[CARE_MOOD] = _scan_mood(mem_conn, rules)
    stats[CARE_OVERWORK] = _scan_overwork(mem_conn, rules)
    stats[CARE_DAILY] = _scan_daily(mem_conn, rules)
    logger.info("关怀信号扫描完成: %s", stats)
    return stats


def list_care_signals(
    mem_conn: sqlite3.Connection,
    *,
    signal_type: str | None = None,
    unconsumed_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """拉取关怀信号（type=care_*）。

    Args:
        signal_type: 类型过滤（care_todo_due 等）；None = 全部 care_*。
        unconsumed_only: True = 只返回未消费（consumed_at IS NULL）。
        limit: 条数上限。
    """
    sql = "SELECT * FROM signal_events WHERE type LIKE 'care_%'"
    params: list[Any] = []
    if signal_type:
        sql += " AND type = ?"
        params.append(signal_type)
    if unconsumed_only:
        sql += " AND consumed_at IS NULL"
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = mem_conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def consume_signal(mem_conn: sqlite3.Connection, event_id: str, agent_id: str | None = None) -> bool:
    """原子认领关怀信号（谁消费谁标记，ST-27 T-57）。

    - 返回 True=本次认领成功；False=已被他人消费（并发抢失败）
    - agent_id 记录认领方，配合 signal_acks 回执溯源
    """
    return signal_dao.mark_consumed(mem_conn, event_id, consumed_by=agent_id)
