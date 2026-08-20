"""storage/signal_dao.py：signal_events + signal_subscribers DAO（v0.4 T9）。

职责：
- signal_events 事件持久化（insert / get / list_since / mark_consumed）
- signal_subscribers 订阅者持久游标（upsert / get / list_unconsumed）

设计依据：§3 / §11.1
- 事件信封 {event_id, type, source, payload, ts}
- consumed_at 为单事件消费标记（订阅者水位另存 signal_subscribers）
- pull 模式持久游标：subscriber.last_signal_id 推进，断线重连补偿用

所有写入使用参数化查询，防 SQL 注入。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- signal_events ----------

def insert_event(
    conn: sqlite3.Connection,
    event_id: str,
    event_type: str,
    source: str,
    payload_json: str,
    ts: str | None = None,
) -> str:
    """持久化事件到 signal_events。

    - payload_json：调用方负责 JSON 序列化（保持 DAO 层无 JSON 依赖）
    - ts 缺省取当前 UTC ISO 时间戳
    - consumed_at 初始为 NULL（未消费）
    """
    e_ts = ts or _now_iso()
    conn.execute(
        """
        INSERT INTO signal_events (event_id, type, source, payload, ts, consumed_at)
        VALUES (?,?,?,?,?,NULL)
        """,
        (event_id, event_type, source, payload_json, e_ts),
    )
    conn.commit()
    return event_id


def get_event(conn: sqlite3.Connection, event_id: str) -> dict | None:
    """返回单条事件或 None。"""
    cur = conn.execute(
        "SELECT * FROM signal_events WHERE event_id=?", (event_id,)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def list_events_since(
    conn: sqlite3.Connection,
    since_ts: str | None = None,
    since_event_id: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """按时间/游标拉取事件（ts ASC）。

    - since_ts：拉取 ts > since_ts 的事件
    - since_event_id：拉取 event_id > since_event_id 的事件（字符串游标式）
    - event_type：可选类型过滤（如 'memory_updated' / 'anomaly_warn'）
    - limit：默认 100
    """
    sql = "SELECT * FROM signal_events WHERE 1=1"
    params: list = []
    if since_ts is not None:
        sql += " AND ts > ?"
        params.append(since_ts)
    if since_event_id is not None:
        sql += " AND event_id > ?"
        params.append(since_event_id)
    if event_type is not None:
        sql += " AND type = ?"
        params.append(event_type)
    sql += " ORDER BY ts ASC LIMIT ?"
    params.append(limit)
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def mark_consumed(
    conn: sqlite3.Connection,
    event_id: str,
    consumed_at: str | None = None,
    consumed_by: str | None = None,
) -> bool:
    """原子认领消费：谁先抢到谁消费（WHERE consumed_at IS NULL）。

    - 返回 True=本次认领成功（rowcount=1）；False=已被他人消费（并发抢失败）
    - consumed_by 记录认领方（agent_id），配合 signal_acks 回执溯源
    - 这是「谁消费谁标记」的安全前提：无条件 UPDATE 会让两个消费者都误判抢到

    注：语义从「标记已消费」升级为「原子认领」，返回值含义相应变化——
    调用方须按 True=抢到 / False=已被抢 处理（care/operations 层已同步）。
    """
    c_at = consumed_at or _now_iso()
    cur = conn.execute(
        "UPDATE signal_events SET consumed_at=?, consumed_by=? "
        "WHERE event_id=? AND consumed_at IS NULL",
        (c_at, consumed_by, event_id),
    )
    conn.commit()
    return cur.rowcount > 0


def ack_signal(
    conn: sqlite3.Connection,
    event_id: str,
    agent_id: str,
    status: str,
    result: str | None = None,
) -> bool:
    """写消费回执（signal_acks 表，幂等 upsert）。

    - status: 'claimed'（认领未处理完）/ 'acked'（处理成功）/ 'failed'（处理失败）
    - claimed 不写 acked_at；acked/failed 写 acked_at
    - 幂等 upsert：同 (event_id, agent_id) 重复回执覆盖为最新状态
    """
    now = _now_iso()
    acked_at = now if status in ("acked", "failed") else None
    conn.execute(
        """
        INSERT INTO signal_acks (event_id, agent_id, status, result, claimed_at, acked_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(event_id, agent_id) DO UPDATE SET
          status=excluded.status, result=excluded.result, acked_at=excluded.acked_at
        """,
        (event_id, agent_id, status, result, now, acked_at),
    )
    conn.commit()
    return True


def purge_expired_signals(
    conn: sqlite3.Connection,
    *,
    anomaly_days: int = 30,
    heartbeat_days: int = 7,
    care_days: int = 7,
) -> dict:
    """TTL 归档：清理超期信号（ST-27 T-57）。

    - 异常类（anomaly_warn / batch_scan_error / dream_error）：保留 anomaly_days 天
    - memory_updated（纯心跳）：保留 heartbeat_days 天
    - care_*：已消费且超 care_days 天（未消费的 care 信号不清理——待 agent 消费）

    信号是衍生数据（非「原件」——原件=记忆/会话），超期物理删除不归档。
    返回 {anomaly, memory_updated, care} 各类删除条数。
    """
    now = datetime.now(timezone.utc)
    anomaly_cutoff = (now - timedelta(days=anomaly_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    heartbeat_cutoff = (now - timedelta(days=heartbeat_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    care_cutoff = (now - timedelta(days=care_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    counts: dict = {}

    cur = conn.execute(
        "DELETE FROM signal_events WHERE type IN "
        "('anomaly_warn','batch_scan_error','dream_error') AND ts < ?",
        (anomaly_cutoff,),
    )
    counts["anomaly"] = cur.rowcount

    cur = conn.execute(
        "DELETE FROM signal_events WHERE type='memory_updated' AND ts < ?",
        (heartbeat_cutoff,),
    )
    counts["memory_updated"] = cur.rowcount

    cur = conn.execute(
        "DELETE FROM signal_events WHERE type LIKE 'care_%' "
        "AND consumed_at IS NOT NULL AND ts < ?",
        (care_cutoff,),
    )
    counts["care"] = cur.rowcount

    conn.commit()
    return counts


# ---------- signal_subscribers ----------

def upsert_subscriber(
    conn: sqlite3.Connection,
    subscriber_id: str,
    last_signal_id: str | None = None,
    last_consumed_ts: str | None = None,
) -> None:
    """插入或更新订阅者持久游标（pull 重连补偿用）。"""
    conn.execute(
        """
        INSERT INTO signal_subscribers (subscriber_id, last_signal_id, last_consumed_ts)
        VALUES (?,?,?)
        ON CONFLICT(subscriber_id) DO UPDATE SET
          last_signal_id=excluded.last_signal_id,
          last_consumed_ts=excluded.last_consumed_ts
        """,
        (subscriber_id, last_signal_id, last_consumed_ts),
    )
    conn.commit()


def get_subscriber(conn: sqlite3.Connection, subscriber_id: str) -> dict | None:
    """返回订阅者游标记录或 None。"""
    cur = conn.execute(
        "SELECT * FROM signal_subscribers WHERE subscriber_id=?",
        (subscriber_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def list_unconsumed(
    conn: sqlite3.Connection,
    subscriber_id: str,
    limit: int = 100,
) -> list[dict]:
    """按订阅者游标拉取未消费事件 + 推进游标。

    - 根据 subscriber.last_signal_id 拉取后续事件（event_id > last_signal_id）
    - 拉取后将游标推进到本次最后一条事件的 event_id 与 ts
    - 若 subscriber 不存在，先创建（last_signal_id=NULL，从头拉取）
    - 返回事件列表（dict），按 ts ASC 排序
    """
    sub = get_subscriber(conn, subscriber_id)
    if sub is None:
        upsert_subscriber(conn, subscriber_id, None, None)
        sub = get_subscriber(conn, subscriber_id)

    last_id = sub["last_signal_id"]
    sql = "SELECT * FROM signal_events"
    params: list = []
    if last_id is not None:
        sql += " WHERE event_id > ?"
        params.append(last_id)
    sql += " ORDER BY ts ASC LIMIT ?"
    params.append(limit)
    cur = conn.execute(sql, params)
    events = [dict(r) for r in cur.fetchall()]

    # 推进游标到最后一条已拉取事件
    if events:
        last = events[-1]
        upsert_subscriber(conn, subscriber_id, last["event_id"], last["ts"])

    return events


# ---------- T-9 收口：signal/engine.py 直查 SQL 迁入 ----------

def get_recent_event_ts(
    conn: sqlite3.Connection,
    event_type: str,
    source: str,
) -> str | None:
    """同源同类型最近一条事件的 ts（publish 的 suppress_hint 用；无则 None）。

    T-9 收口：原 signal/engine.py::publish 的直接 SQL
    （``SELECT ts ... ORDER BY ts DESC LIMIT 1``）迁入本 DAO，
    engine 层不再写 SQL。
    """
    row = conn.execute(
        "SELECT ts FROM signal_events WHERE type=? AND source=? "
        "ORDER BY ts DESC LIMIT 1",
        (event_type, source),
    ).fetchone()
    if row is None:
        return None
    return row["ts"] if isinstance(row, sqlite3.Row) else row[0]


def count_events_before_ts(
    conn: sqlite3.Connection,
    ts: str,
) -> int:
    """统计 ``ts <= 给定时间戳`` 的事件数（重放窗口超窗摘要用）。

    T-9 收口：原 signal/engine.py::get_replay_window_events 的
    ``SELECT COUNT(*) ... WHERE ts <= ?`` 迁入本 DAO。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM signal_events WHERE ts <= ?", (ts,)
    ).fetchone()
    return row["c"] if row else 0
