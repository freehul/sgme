"""D-73 召回加固接线回归：梦境 TTL 判定必须按召回次数放宽有效期。

设计（D-73）：记忆被检索/注入命中越多（recall_count 越大），有效期按幂律
放宽，上限 3×；``forget_request`` 记忆豁免放大（遗忘请求必须按原期生效）。

本用例锁住 dream._mark_expired_ttl 的接线——把倍率分支去掉，
``test_high_recall_postpones_expiry`` 即红。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine import dream as dream_mod


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    from sgme import config

    cfg = config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    ttl_days: int,
    age_days: int,
    recall_count: int = 0,
    memory_type: str = "fact",
) -> None:
    ts = _iso_days_ago(age_days)
    conn.execute(
        """
        INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, status,
                              ttl_days, created_at, updated_at, content_seg)
        VALUES (?, ?, ?, 50, 0, 'active', ?, ?, ?, ?)
        """,
        (memory_id, f"测试记忆 {memory_id}", memory_type, ttl_days, ts, ts, f"测试记忆 {memory_id}"),
    )
    if recall_count:
        conn.execute(
            "INSERT INTO memory_stats (memory_id, last_recalled_at, recall_count, last_injected_at) "
            "VALUES (?, ?, ?, ?)",
            (memory_id, ts, recall_count, ts),
        )
    conn.commit()


def _status(conn: sqlite3.Connection, memory_id: str) -> str:
    row = conn.execute("SELECT status FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    return row[0] if row else "missing"


def test_zero_recall_expires_as_before(mem_conn):
    """无召回（倍率 1.0）→ 按原 TTL 过期，行为与旧版一致。"""
    _seed(mem_conn, "m-zero", ttl_days=30, age_days=40, recall_count=0)
    changed = dream_mod._mark_expired_ttl(mem_conn)
    assert changed == 1
    assert _status(mem_conn, "m-zero") == "expired"


def test_high_recall_postpones_expiry(mem_conn):
    """召回 50 次（倍率触顶 3× → 90 天）→ 40 天的记忆不该过期。"""
    _seed(mem_conn, "m-hot", ttl_days=30, age_days=40, recall_count=50)
    changed = dream_mod._mark_expired_ttl(mem_conn)
    assert changed == 0, "高召回记忆被误判过期——倍率分支未生效"
    assert _status(mem_conn, "m-hot") == "active"


def test_forget_request_exempt_from_multiplier(mem_conn):
    """forget_request 豁免放大：即使召回高，仍按原 TTL 过期。"""
    _seed(
        mem_conn, "m-forget", ttl_days=30, age_days=40,
        recall_count=50, memory_type="forget_request",
    )
    changed = dream_mod._mark_expired_ttl(mem_conn)
    assert changed == 1, "遗忘请求被高召回延长了有效期——豁免分支未生效"
    assert _status(mem_conn, "m-forget") == "expired"


def test_mixed_batch(mem_conn):
    """混合批次：仅到期的被标记，其余保持 active。"""
    _seed(mem_conn, "m-cold", ttl_days=30, age_days=40, recall_count=0)
    _seed(mem_conn, "m-hot", ttl_days=30, age_days=40, recall_count=50)
    _seed(mem_conn, "m-fresh", ttl_days=30, age_days=1, recall_count=0)
    changed = dream_mod._mark_expired_ttl(mem_conn)
    assert changed == 1
    assert _status(mem_conn, "m-cold") == "expired"
    assert _status(mem_conn, "m-hot") == "active"
    assert _status(mem_conn, "m-fresh") == "active"
