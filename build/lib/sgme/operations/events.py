"""operations/events.py：信号事件操作（v0.8 T-8 events 链路）。

三段式结构（照 operations/health.py 样板）：
1. 常量/私有工具函数（本模块内聚，不外泄）
2. ``events_pull`` / ``events_list`` / ``events_stream`` 操作函数：
   显式接参，返回**协议无关的信息超集**
3. ``http_payload(data)`` 投影函数：把超集裁剪成 HTTP 历史契约形态

职责迁移（v0.8 T-8）
--------------------
改造前业务逻辑散在 ``server/routes_events.py``（路由直调 ``signal.engine``），
本次全部下沉本模块；路由退化为纯协议翻译。SSE 流的轮询 / keepalive / 断连
生成器逻辑一并收进 ``events_stream``（yield 协议无关的流项），
SSE 帧的最终组帧仍属协议翻译，留在路由侧——帧格式与改造前逐字节一致。

流式操作说明
------------
``events_stream`` 是异步生成器工厂（不返回 OperationResult），原因：
流式操作逐项 yield，不存在单一的「结果 data」可包装；且任务验收点 ①
「操作函数返回 OperationResult」针对 pull/list 两个非流式操作。
错误处理与改造前一致——不在此加 catch-all，异常原样上抛由入口层兜底，
仅保留 ``CancelledError`` 正常退出语义（客户端断连）。

依赖：只调 ``sgme.signal.engine.pull``（engine 是禁区，只读不改）与
``sgme.data.signal_dao``（Last-Event-ID 重连补偿需直接读写订阅者游标）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sgme.data import signal_dao
from sgme.operations.errors import ERR_INTERNAL, OperationResult
from sgme.signal import engine as signal_engine

# ---------- 常量 ----------
# （原硬编码在 routes_events.py，v0.8 收敛到此单点）

# SSE 推送间隔（秒）
SSE_POLL_INTERVAL_SEC = 1
# SSE keepalive 间隔（秒）
SSE_KEEPALIVE_INTERVAL_SEC = 15
# 流式拉取单批上限（原流循环内硬编码 100）
STREAM_PULL_LIMIT = 100
# 契约 §4.5 兼容订阅者（GET /v1/events 固定使用，原路由内硬编码 "contract-legacy"）
CONTRACT_LEGACY_SUBSCRIBER = "contract-legacy"
# keepalive 哨兵：协议无关的流内标记，入口层投影为 SSE 注释帧 ": keepalive\n\n"
KEEPALIVE_MARK: dict[str, Any] = {"keepalive": True}


def _apply_last_event_id(
    mem_conn: sqlite3.Connection,
    subscriber_id: str,
    last_event_id: str,
) -> None:
    """Last-Event-ID 重连补偿：覆盖订阅者持久游标（保留 last_consumed_ts）。

    语义与改造前 ``routes_events.event_stream`` 逐行一致：
    - 订阅者不存在时 prev_ts=None
    - 仅覆盖 last_signal_id，last_consumed_ts 原样保留
    """
    sub = signal_dao.get_subscriber(mem_conn, subscriber_id)
    prev_ts = sub.get("last_consumed_ts") if sub else None
    signal_dao.upsert_subscriber(mem_conn, subscriber_id, last_event_id, prev_ts)


# ---------- 操作函数 ----------

def events_pull(
    mem_conn: sqlite3.Connection,
    subscriber_id: str,
    last_signal_id: str | None = None,
    limit: int = 100,
) -> OperationResult:
    """带持久游标拉取未消费事件（GET /v1/events/pull 业务）。

    Args:
        mem_conn: memory.db 连接（signal_events / signal_subscribers 表）。
        subscriber_id: 订阅者标识（首次自动创建）。
        last_signal_id: 可选游标，覆盖订阅者持久游标（None 用持久游标）。
        limit: 单批上限（路由层 Query 已限制 1..1000）。

    Returns:
        OperationResult(ok=True)，data 为协议无关超集：
        - events: 事件信封列表 [{event_id, type, source, payload, ts}, ...]
        - next_cursor: (ts, event_id) 复合序最大值的 event_id（无事件时 None）
        HTTP 响应体与改造前逐字段一致，投影见 http_payload。
    """
    result = signal_engine.pull(
        mem_conn=mem_conn,
        subscriber_id=subscriber_id,
        last_signal_id=last_signal_id,
        limit=limit,
    )
    return OperationResult.succeed(result)


def events_list(
    mem_conn: sqlite3.Connection,
    after: str | None = None,
    limit: int = 100,
) -> OperationResult:
    """契约 §4.5 兼容：GET /v1/events?after={last_event_id}（业务）。

    等价 events_pull，subscriber_id 固定为契约兼容订阅者
    （原 ``routes_events.events_after`` 行为逐行保留，含 ``after or None`` 归一）。
    """
    return events_pull(
        mem_conn,
        subscriber_id=CONTRACT_LEGACY_SUBSCRIBER,
        last_signal_id=after or None,
        limit=limit,
    )


def events_stream(
    mem_conn: sqlite3.Connection,
    subscriber_id: str,
    last_event_id: str | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """SSE 轮询流生成器工厂（GET /v1/events/stream 业务）。

    生成器逻辑（改造前在路由内联的 ``generate()``）整体收进本函数：
    - 每 ``SSE_POLL_INTERVAL_SEC`` 秒轮询一次未消费事件
    - 无事件累计达到 ``SSE_KEEPALIVE_INTERVAL_SEC`` 时 yield 一次 keepalive 哨兵
    - 客户端断连（is_disconnected 回调返回 True）时退出循环
    - Last-Event-ID 重连补偿在**调用时立即生效**（进入流之前），
      与改造前路由同步执行 upsert 的时序一致

    Args:
        mem_conn: memory.db 连接。
        subscriber_id: 订阅者标识。
        last_event_id: Last-Event-ID 头（可选）；非空则覆盖订阅者持久游标。
        is_disconnected: 入口层注入的断连检测回调（如 ``request.is_disconnected``），
            None 表示不断连（便于 operations 层独立测试）。

    Yields:
        协议无关流项（由入口层投影为 SSE 帧）：
        - 事件信封 dict {event_id, type, source, payload, ts}
        - keepalive 哨兵 ``KEEPALIVE_MARK``

    注：同步的 ``signal_engine.pull`` 放线程池执行，避免阻塞事件循环
    （改造前行为保留）。
    """
    # Last-Event-ID 重连补偿：覆盖订阅者持久游标（调用时立即生效）
    if last_event_id:
        _apply_last_event_id(mem_conn, subscriber_id, last_event_id)

    async def _generate() -> AsyncIterator[dict[str, Any]]:
        keepalive_counter = 0
        try:
            while True:
                if is_disconnected is not None and await is_disconnected():
                    break

                # 用 signal_engine.pull 拉取未消费事件（带 (ts, event_id)
                # 复合游标，避免 UUIDv4 同秒事件重取）。同步调用放线程池。
                result = await asyncio.to_thread(
                    signal_engine.pull, mem_conn, subscriber_id, None, STREAM_PULL_LIMIT
                )
                events = result["events"]

                for e in events:
                    yield e

                # 无事件时按 keepalive 周期发送哨兵
                if not events:
                    keepalive_counter += 1
                    if (
                        keepalive_counter * SSE_POLL_INTERVAL_SEC
                        >= SSE_KEEPALIVE_INTERVAL_SEC
                    ):
                        yield KEEPALIVE_MARK
                        keepalive_counter = 0

                await asyncio.sleep(SSE_POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            # 客户端断开，正常退出
            raise

    return _generate()


def events_consume_all(
    mem_conn: sqlite3.Connection,
    event_type: str | None = None,
    subscriber_id: str | None = None,
    consumed_by: str | None = None,
) -> OperationResult:
    """批量清空/全部消费信号（T-87）。

    语义与 pull 对齐（标记 + 游标双视角）：
    - 全部未消费事件标记 consumed_at/consumed_by（幂等，二次调用 consumed=0）
    - event_type 可选类型精确过滤（如 'anomaly_warn'）；None = 全部类型
    - subscriber_id 提供时，同步把该订阅者持久游标推进到最新事件
      （(ts, event_id) 复合序最大值，与 signal_engine.pull 推进逻辑一致），
      使 pull/SSE 视角一并清空
    - consumed_by 记录清空方（agent_id）

    Returns:
        OperationResult(ok=True)，data:
        {consumed: 本次标记条数, type: 过滤类型或 None, subscriber_id: 或 None}
    """
    from sgme.data import signal_dao

    try:
        consumed = signal_dao.mark_all_consumed(
            mem_conn, event_type=event_type, consumed_by=consumed_by,
        )
        # 可选：推进订阅者持久游标到最新（pull 视角清空）
        if subscriber_id is not None:
            latest = signal_dao.get_latest_event(mem_conn)
            if latest is not None:
                signal_dao.upsert_subscriber(
                    mem_conn, subscriber_id, latest["event_id"], latest["ts"],
                )
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"批量消费失败: {e}")
    return OperationResult.succeed({
        "consumed": consumed,
        "type": event_type,
        "subscriber_id": subscriber_id,
    })


# ---------- 投影函数 ----------

def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP 历史契约形态（v0.4 逐字段等价）。

    pull/list 的 data 本身就是协议无关的事件信封集合，HTTP 响应体与改造前
    逐字段一致，故投影为恒等函数；保留此函数是为三段式结构的显式性
    （路由统一经它出口）。events 当前无 MCP 入口，暂不写 mcp_payload；
    未来接入 MCP 时若形态有差异再补充。
    """
    return data
