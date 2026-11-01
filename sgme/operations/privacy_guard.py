# -*- coding: utf-8 -*-
"""sgme/operations/privacy_guard.py：角色隐私层访问控制（D-65）。

角色卡可在 data 层声明 ``privacy_agents`` 白名单（允许装配该角色的
agent_id 列表）；装配与读取时按 API key 反查出的 agent_id 校验，未授权
拒绝（入口层翻译为 403），越权尝试写 ``security_log`` 审计，当日累计
≥ threshold（默认 3）次可触发告警（should_alert 返回 True，入口层消费）。

默认策略（契约 D-65）：
- ``privacy_agents`` 缺失或为空数组 → 未启用隔离，放行（reason="no_policy"），
  ``resolve_allowed_agents`` 返回空集合（{"__owner__"} 语义不物化）；
- 声明了非空数组 → 仅集合内 agent_id 放行，其余拒绝。

纯叶子约束：不 import config / fastapi；conn 由调用方显式传入（operations
层铁律）；所有写操作自行 commit。``security_log`` 表不在 db.py 初始化清单
里，由 ``ensure_security_log`` 以 CREATE TABLE IF NOT EXISTS 幂等自建。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("sgme.operations.privacy_guard")

# 审计事件名（契约固定）
EVENT_DENIED = "privacy_access_denied"

# check_access 的拒绝/放行原因
REASON_NO_POLICY = "no_policy"          # 未启用隔离（缺字段或空数组）
REASON_WHITELIST = "whitelist"          # agent 在白名单内
REASON_NOT_IN_WHITELIST = "not_in_whitelist"  # 启用隔离且 agent 不在白名单


def ensure_security_log(conn: sqlite3.Connection) -> None:
    """幂等自建 security_log 审计表（db.py 初始化清单之外的自建表）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            event TEXT,
            role_id TEXT,
            agent_id TEXT,
            detail TEXT
        )
        """
    )
    # 当日统计（should_alert）按 role+agent+时间过滤，避免全表扫描
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_log_role_agent_ts"
        " ON security_log (role_id, agent_id, ts)"
    )
    conn.commit()


def _card_data(role_card: dict) -> dict:
    """取角色卡 data 层：CC V2 双层结构取 ``data``，扁平 dict 原样兼容。"""
    if not isinstance(role_card, dict):
        return {}
    data = role_card.get("data")
    return data if isinstance(data, dict) else role_card


def resolve_allowed_agents(role_card: dict) -> set[str]:
    """返回允许装配该角色的 agent_id 集合。

    规则（契约 D-65）：有 ``privacy_agents`` 数组则用该数组；缺失或空数组
    回退 {"__owner__"} 语义——返回空集合，由 check_access 走缺省放行逻辑。
    """
    agents = _card_data(role_card).get("privacy_agents")
    if not isinstance(agents, list):
        return set()
    # 空白数组与含空白条目的数组都不放行空白身份
    return {str(a).strip() for a in agents if str(a).strip()}


def check_access(role_card: dict, agent_id: str) -> tuple[bool, str]:
    """判定 agent_id 可否装配该角色。返回 (是否允许, 拒绝原因)。

    缺省规则：无 privacy_agents 字段视为未启用隔离，放行（reason="no_policy"）。
    """
    allowed = resolve_allowed_agents(role_card)
    if not allowed:
        return True, REASON_NO_POLICY
    if agent_id in allowed:
        return True, REASON_WHITELIST
    return False, REASON_NOT_IN_WHITELIST


def record_denial(
    conn: sqlite3.Connection,
    role_id: str,
    agent_id: str,
    detail: str = "",
) -> None:
    """记一条越权尝试审计（event='privacy_access_denied'），自行 commit。"""
    ensure_security_log(conn)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO security_log (ts, event, role_id, agent_id, detail)"
        " VALUES (?, ?, ?, ?, ?)",
        (ts, EVENT_DENIED, str(role_id), str(agent_id), str(detail or "")),
    )
    conn.commit()
    logger.warning("角色装配越权已记录: role=%s agent=%s", role_id, agent_id)


def should_alert(
    conn: sqlite3.Connection,
    role_id: str,
    agent_id: str,
    threshold: int = 3,
) -> bool:
    """当日（UTC 日期）该 role+agent 的拒绝次数 >= threshold → 告警。

    表不存在（零记录）视为不告警；跨天自然重置（统计窗口只覆盖今天）。
    """
    now = datetime.now(timezone.utc)
    day_start = now.strftime("%Y-%m-%dT00:00:00Z")
    day_end = (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM security_log"
            " WHERE event = ? AND role_id = ? AND agent_id = ?"
            " AND ts >= ? AND ts < ?",
            (EVENT_DENIED, str(role_id), str(agent_id), day_start, day_end),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        # 表未建过：无拒绝记录，谈不上告警
        return False
    return n >= threshold
