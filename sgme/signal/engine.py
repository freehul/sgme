"""signal/engine.py：信号引擎（v0.4 T11）。

职责：
- publish：发布事件到 signal_events（发布侧不做合并过滤，抑制窗口归消费端）
- pull：订阅者带持久游标拉取（首次自动创建订阅者）
- get_replay_window_events：重放窗口内事件（默认 1 小时），超窗口合并为 _summary

设计依据：§3 / §11.1
- 事件信封 {event_id, type, source, payload, ts}
- suppress_hint：同源同类型最近发布时间（30 分钟内）附入 payload，辅助消费端抑制
- replay_window：超窗口事件合并为单条 _summary 信号，避免历史洪泛
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone


# 重放窗口（秒）：默认 1 小时
REPLAY_WINDOW_SECONDS = 3600

# 抑制窗口（秒）：同源同类型 30 分钟内重复发布时附 suppress_hint
SUPPRESS_WINDOW_SECONDS = 1800


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime | None:
    """解析 ISO 8601 时间戳为 aware datetime；失败返回 None。"""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def publish(
    event_type: str,
    source: str,
    payload: dict,
    mem_conn: sqlite3.Connection,
) -> str:
    """发布事件到 signal_events 表。

    - event_type: 'memory_updated' | 'anomaly_warn'
    - source: 事件来源标识（如 'refine', 'l2', 'health'）
    - payload: 事件载荷 dict（内部 json.dumps 序列化，ensure_ascii=False）
    - 发布侧不做合并过滤（抑制窗口归 SCSM 消费端，§11.1）
    - 可选附 suppress_hint（同源同类型最近发布时间）辅助消费端抑制
    - 返回 event_id
    """
    event_id = str(uuid.uuid4())
    ts = _now_iso()

    from sgme.data import signal_dao

    # 抑制提示：查同源同类型最近一条事件，30 分钟内则附 suppress_hint
    # （T-9 收口：直查 SQL 迁入 signal_dao.get_recent_event_ts）
    last_ts = signal_dao.get_recent_event_ts(mem_conn, event_type, source)
    if last_ts is not None:
        last_dt = _parse_iso(last_ts)
        now_dt = _parse_iso(ts)
        if last_dt and now_dt:
            delta = (now_dt - last_dt).total_seconds()
            if 0 <= delta <= SUPPRESS_WINDOW_SECONDS:
                # 复制 payload 避免修改入参
                payload = {**payload, "suppress_hint": last_ts}

    payload_json = json.dumps(payload, ensure_ascii=False)

    signal_dao.insert_event(
        conn=mem_conn,
        event_id=event_id,
        event_type=event_type,
        source=source,
        payload_json=payload_json,
        ts=ts,
    )
    return event_id


def claim(
    mem_conn: sqlite3.Connection,
    event_id: str,
    agent_id: str,
) -> bool:
    """原子认领信号：谁先抢到谁消费（ST-27 T-57 三层消费模型第 2 层）。

    - 返回 True=本次认领成功（抢到）；False=已被他人消费（并发抢失败）
    - 认领成功后调用方应写回执（signal_dao.ack_signal）报告处理结果，
      失败/半途而废可再 ack_signal(status='failed') 释放语义
    """
    from sgme.data import signal_dao
    return signal_dao.mark_consumed(mem_conn, event_id, consumed_by=agent_id)


def pull(
    mem_conn: sqlite3.Connection,
    subscriber_id: str,
    last_signal_id: str | None = None,
    limit: int = 100,
) -> dict:
    """拉取未消费事件（带持久游标）。

    - subscriber_id: 订阅者标识（首次自动创建）
    - last_signal_id: 可选游标（覆盖订阅者持久游标）；None 则用持久游标
    - 返回 {events: [...], next_cursor: str|None}

    实现说明：UUIDv4 非时序可排序，list_unconsumed 的 event_id 游标在
    同秒事件场景下会漏取/重取，故改用 (ts, event_id) 复合游标手动过滤。
    """
    from sgme.data import signal_dao

    # 获取或创建订阅者
    sub = signal_dao.get_subscriber(mem_conn, subscriber_id)
    if sub is None:
        signal_dao.upsert_subscriber(mem_conn, subscriber_id, None, None)
        sub = signal_dao.get_subscriber(mem_conn, subscriber_id)

    # 确定游标：last_signal_id 覆盖 > 持久游标
    if last_signal_id is not None:
        cursor_event = signal_dao.get_event(mem_conn, last_signal_id)
        cursor_ts = cursor_event["ts"] if cursor_event else sub.get("last_consumed_ts")
        cursor_id = last_signal_id
    else:
        cursor_ts = sub.get("last_consumed_ts")
        cursor_id = sub.get("last_signal_id")

    # 拉取全部事件（ts ASC），然后在 Python 中按 (ts, event_id) 复合游标过滤。
    # 原因：UUIDv4 非时序可排序，list_events_since 的 event_id 游标在同秒事件
    # 场景下会漏取/重取；改用 ts 严格大于、或 ts 等于且 event_id 大于 游标。
    all_events = signal_dao.list_events_since(mem_conn, limit=10000)
    if cursor_ts is not None and cursor_id is not None:
        events = [
            e for e in all_events
            if e["ts"] > cursor_ts
            or (e["ts"] == cursor_ts and e["event_id"] > cursor_id)
        ]
    else:
        events = all_events
    events = events[:limit]

    # 推进游标到本次拉取事件中 (ts, event_id) 复合序最大值。
    # 同秒事件场景下 events[-1]（ts ASC 末条）未必 event_id 最大，
    # 取复合序最大值确保所有返回事件都被游标覆盖，避免下次重取。
    if events:
        last = max(events, key=lambda e: (e["ts"], e["event_id"]))
        signal_dao.upsert_subscriber(
            mem_conn, subscriber_id, last["event_id"], last["ts"]
        )

    # 规范化输出：反序列化 payload 为 dict
    out_events: list[dict] = []
    for e in events:
        try:
            data = json.loads(e["payload"]) if e.get("payload") else {}
        except (ValueError, TypeError):
            data = {}
        out_events.append({
            "event_id": e["event_id"],
            "type": e["type"],
            "source": e["source"],
            "payload": data,
            "ts": e["ts"],
        })

    # next_cursor 与持久化游标一致（复合序最大值的 event_id）
    next_cursor = None
    if out_events:
        cursor_event = max(out_events, key=lambda e: (e["ts"], e["event_id"]))
        next_cursor = cursor_event["event_id"]
    return {"events": out_events, "next_cursor": next_cursor}


def get_replay_window_events(
    mem_conn: sqlite3.Connection,
    since_ts: str,
    limit: int = 1000,
) -> list[dict]:
    """获取重放窗口内的事件（默认 1 小时）。

    超窗口事件合并为摘要信号（type='_summary'）。
    - since_ts: 起始时间戳（不含），仅作上限边界参考；窗口下界 = now - REPLAY_WINDOW_SECONDS
    - 实际返回：[<_summary?>, <event>, <event>, ...] 按 ts ASC
    """
    now_dt = datetime.now(timezone.utc)
    window_lower = now_dt - timedelta(seconds=REPLAY_WINDOW_SECONDS)
    window_lower_str = window_lower.strftime("%Y-%m-%dT%H:%M:%SZ")

    from sgme.data import signal_dao

    # 拉取窗口内全部事件（ts > window_lower；T-9 收口：走 DAO，engine 零 SQL）
    in_window = signal_dao.list_events_since(
        mem_conn, since_ts=window_lower_str, limit=limit
    )

    # 统计超窗口事件数（ts <= window_lower；T-9 收口：走 DAO）
    old_count = signal_dao.count_events_before_ts(mem_conn, window_lower_str)

    out: list[dict] = []
    if old_count > 0:
        out.append({
            "event_id": "_summary",
            "type": "_summary",
            "source": "engine",
            "payload": {"old_events_count": old_count, "window_lower": window_lower_str},
            "ts": window_lower_str,
        })

    for e in in_window:
        try:
            data = json.loads(e["payload"]) if e.get("payload") else {}
        except (ValueError, TypeError):
            data = {}
        out.append({
            "event_id": e["event_id"],
            "type": e["type"],
            "source": e["source"],
            "payload": data,
            "ts": e["ts"],
        })

    return out
