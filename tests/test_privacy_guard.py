# -*- coding: utf-8 -*-
"""tests/test_privacy_guard.py：角色隐私层访问控制测试（D-65）。

覆盖：白名单命中 / 未授权拒绝 / 无 privacy_agents 时放行（no_policy）/
空白数组缺省放行（{"__owner__"} 语义）/ 当日 3 次触发告警 / 跨天重置 /
record_denial 落库自提交与自建表。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sgme.operations.privacy_guard import (
    check_access,
    ensure_security_log,
    record_denial,
    resolve_allowed_agents,
    should_alert,
)

# 契约固定的事件名
EVENT = "privacy_access_denied"


# ---------- 夹具 ----------

def _cc_card(privacy_agents=None, with_field: bool = True) -> dict:
    """构造 CC V2 结构角色卡（privacy_agents 挂 data 层，与 care/roles.py 对齐）。"""
    data: dict = {"name": "测试角色", "description": "用于隐私层测试"}
    if with_field:
        data["privacy_agents"] = privacy_agents
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


@pytest.fixture
def conn(tmp_path):
    """文件库连接（另开第二个连接验证 record_denial 已自提交）。"""
    c = sqlite3.connect(tmp_path / "sgme_test.db")
    yield c
    c.close()


# ---------- resolve_allowed_agents ----------

def test_resolve_whitelist_hit():
    """声明了 privacy_agents 数组 → 原样返回集合。"""
    card = _cc_card(privacy_agents=["agt_a", "agt_b"])
    assert resolve_allowed_agents(card) == {"agt_a", "agt_b"}


def test_resolve_missing_field_returns_empty():
    """缺失 privacy_agents → 返回空集合（由 check_access 走缺省放行）。"""
    assert resolve_allowed_agents(_cc_card()) == set()


def test_resolve_empty_list_returns_empty():
    """空数组 → 回退 {"__owner__"} 语义，返回空集合而非物化 {"__owner__"}。"""
    assert resolve_allowed_agents(_cc_card(privacy_agents=[])) == set()


def test_resolve_flat_card_supported():
    """扁平 dict（非 CC V2 嵌套）也兼容：顶层键直接读。"""
    assert resolve_allowed_agents({"privacy_agents": ["agt_x"]}) == {"agt_x"}


def test_resolve_drops_blank_entries():
    """空白/纯空格条目不进白名单（不能靠空串拿到装配权）。"""
    card = _cc_card(privacy_agents=["agt_a", "  ", ""])
    assert resolve_allowed_agents(card) == {"agt_a"}


# ---------- check_access ----------

def test_check_access_whitelist_hit():
    """agent 在白名单内 → 放行。"""
    ok, _ = check_access(_cc_card(privacy_agents=["agt_a", "agt_b"]), "agt_a")
    assert ok is True


def test_check_access_unauthorized_denied():
    """agent 不在白名单内 → 拒绝，且拒绝原因非空（供 403 与审计）。"""
    ok, reason = check_access(_cc_card(privacy_agents=["agt_a"]), "agt_evil")
    assert ok is False
    assert reason


def test_check_access_no_policy_allows():
    """无 privacy_agents 字段 → 未启用隔离，放行（契约指定 reason="no_policy"）。"""
    ok, reason = check_access(_cc_card(), "any_agent")
    assert ok is True
    assert reason == "no_policy"


def test_check_access_empty_policy_allows():
    """空数组 → {"__owner__"} 语义：check_access 走缺省放行逻辑。"""
    ok, reason = check_access(_cc_card(privacy_agents=[]), "any_agent")
    assert ok is True
    assert reason == "no_policy"


# ---------- record_denial ----------

def test_record_denial_writes_and_commits(conn, tmp_path):
    """写 event='privacy_access_denied'，且第二个连接可读（证明自己 commit）。"""
    record_denial(conn, "role_a", "agt_evil", detail="装配被拒")
    conn2 = sqlite3.connect(tmp_path / "sgme_test.db")
    try:
        row = conn2.execute(
            "SELECT event, role_id, agent_id, detail, ts FROM security_log"
        ).fetchone()
    finally:
        conn2.close()
    assert row[:4] == (EVENT, "role_a", "agt_evil", "装配被拒")
    # ts 以 UTC 日期开头（should_alert 的当日统计依赖该前缀）
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert row[4].startswith(today)


def test_record_denial_self_ensure_table(conn):
    """未先调 ensure_security_log 也能直接记拒绝（写前自建表，幂等）。"""
    record_denial(conn, "role_a", "agt_evil")
    n = conn.execute("SELECT COUNT(*) FROM security_log").fetchone()[0]
    assert n == 1


# ---------- should_alert ----------

def test_should_alert_below_threshold(conn):
    """当日 2 次（< 默认阈值 3）→ 不告警。"""
    for _ in range(2):
        record_denial(conn, "role_a", "agt_evil")
    assert should_alert(conn, "role_a", "agt_evil") is False


def test_should_alert_triggers_at_three(conn):
    """当日 3 次（>= 默认阈值 3）→ 告警。"""
    for _ in range(3):
        record_denial(conn, "role_a", "agt_evil")
    assert should_alert(conn, "role_a", "agt_evil") is True


def test_should_alert_custom_threshold(conn):
    """threshold 可调：2 次配 threshold=2 也触发。"""
    for _ in range(2):
        record_denial(conn, "role_a", "agt_evil")
    assert should_alert(conn, "role_a", "agt_evil", threshold=2) is True


def test_should_alert_isolated_per_role_and_agent(conn):
    """按 role+agent 维度隔离统计：其他 role / 其他 agent 不跨累计。"""
    for _ in range(3):
        record_denial(conn, "role_a", "agt_evil")
    assert should_alert(conn, "role_b", "agt_evil") is False
    assert should_alert(conn, "role_a", "agt_other") is False


def test_should_alert_no_table_returns_false(conn):
    """表从未建过（零拒绝记录）→ 不告警也不报错。"""
    assert should_alert(conn, "role_a", "agt_evil") is False


def test_should_alert_cross_day_reset(conn):
    """跨天重置：昨天 3 次不累计，今天重新计数。"""
    ensure_security_log(conn)
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    def _insert(day: str) -> None:
        for _ in range(3):
            conn.execute(
                "INSERT INTO security_log (ts, event, role_id, agent_id, detail)"
                " VALUES (?, ?, ?, ?, '')",
                (f"{day}T10:00:00Z", EVENT, "role_a", "agt_evil"),
            )
        conn.commit()

    _insert(yesterday)
    # 昨天已达阈值，但跨天不累计 → 不告警
    assert should_alert(conn, "role_a", "agt_evil") is False

    _insert(today)
    # 今天 3 次 → 触发
    assert should_alert(conn, "role_a", "agt_evil") is True
