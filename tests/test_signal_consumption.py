"""ST-27 T-57 测试：信号消费三层模型（原子认领 / 回执 / TTL 归档）。

覆盖：
- mark_consumed 原子认领（谁抢到谁消费 + consumed_by 溯源）
- signal.engine.claim 封装
- ack_signal 回执 upsert（claimed → acked 覆盖）
- purge_expired_signals TTL 分级清理（异常类 30d / memory_updated 7d / care 消费后 7d）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, signal_dao
from sgme.signal import engine as signal_engine


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _iso_minus_days(days: int) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 1. 原子认领 ----------

def test_mark_consumed_atomic_claim(mem_conn):
    """原子认领：第一次抢到 True，第二次 False（已被消费），consumed_by 溯源。"""
    eid = signal_engine.publish("memory_updated", "refine", {"n": 1}, mem_conn)

    ok = signal_dao.mark_consumed(mem_conn, eid, consumed_by="dsh")
    assert ok is True

    # 第二次抢 → False（consumed_at 已非 NULL）
    ok2 = signal_dao.mark_consumed(mem_conn, eid, consumed_by="trae")
    assert ok2 is False

    # consumed_by 记录第一个抢到的
    e = signal_dao.get_event(mem_conn, eid)
    assert e["consumed_by"] == "dsh"
    assert e["consumed_at"] is not None


def test_claim_engine_encapsulation(mem_conn):
    """signal.engine.claim 封装原子认领。"""
    eid = signal_engine.publish("memory_updated", "refine", {"n": 1}, mem_conn)
    assert signal_engine.claim(mem_conn, eid, "dsh") is True
    assert signal_engine.claim(mem_conn, eid, "trae") is False


# ---------- 2. 回执 ----------

def test_ack_signal_upsert(mem_conn):
    """回执 upsert：claimed → acked 覆盖，acked_at 落库。"""
    eid = signal_engine.publish("memory_updated", "refine", {"n": 1}, mem_conn)

    signal_dao.ack_signal(mem_conn, eid, "dsh", "claimed")
    signal_dao.ack_signal(mem_conn, eid, "dsh", "acked", result="已转告用户")

    row = mem_conn.execute(
        "SELECT * FROM signal_acks WHERE event_id=? AND agent_id=?", (eid, "dsh")
    ).fetchone()
    assert row is not None
    assert row["status"] == "acked"
    assert row["result"] == "已转告用户"
    assert row["acked_at"] is not None


def test_ack_signal_claimed_no_acked_at(mem_conn):
    """claimed 状态不写 acked_at。"""
    eid = signal_engine.publish("memory_updated", "refine", {"n": 1}, mem_conn)
    signal_dao.ack_signal(mem_conn, eid, "dsh", "claimed")
    row = mem_conn.execute(
        "SELECT * FROM signal_acks WHERE event_id=? AND agent_id=?", (eid, "dsh")
    ).fetchone()
    assert row["status"] == "claimed"
    assert row["acked_at"] is None


# ---------- 3. TTL 归档 ----------

def test_purge_expired_signals(mem_conn):
    """TTL 分级清理：超期异常/心跳/已消费 care 被删，未超期 care 保留。"""
    # 超期异常类（35 天前）
    signal_dao.insert_event(mem_conn, "a1", "anomaly_warn", "health", "{}", _iso_minus_days(35))
    signal_dao.insert_event(mem_conn, "a2", "batch_scan_error", "batch_scan", "{}", _iso_minus_days(35))
    signal_dao.insert_event(mem_conn, "a3", "dream_error", "dream", "{}", _iso_minus_days(35))
    # 超期 memory_updated（10 天前）
    signal_dao.insert_event(mem_conn, "m1", "memory_updated", "refine", "{}", _iso_minus_days(10))
    # 超期已消费 care（10 天前）
    old_care_ts = _iso_minus_days(10)
    signal_dao.insert_event(mem_conn, "c1", "care_todo_due", "care", "{}", old_care_ts)
    mem_conn.execute("UPDATE signal_events SET consumed_at=? WHERE event_id='c1'", (old_care_ts,))
    # 未超期未消费 care（1 天前）—— 保留（待 agent 消费）
    signal_dao.insert_event(mem_conn, "c2", "care_mood", "care", "{}", _iso_minus_days(1))
    mem_conn.commit()

    counts = signal_dao.purge_expired_signals(mem_conn)

    assert counts["anomaly"] == 3
    assert counts["memory_updated"] == 1
    assert counts["care"] == 1
    # 未超期未消费 care 保留
    assert signal_dao.get_event(mem_conn, "c2") is not None
    # 已消费 care 被删
    assert signal_dao.get_event(mem_conn, "c1") is None
