"""server/routes_events.py：信号事件端点（v0.8 T-8：纯协议翻译）。

v0.8：业务逻辑已全部下沉 ``sgme.operations.events``
（events_pull / events_list / events_stream），本模块只做协议翻译——
从 app.state 取依赖 → 调 operation → 投影/组帧。
响应格式与改造前逐字段一致（含 SSE 事件帧格式，不得改变）。

- GET /v1/events/stream  SSE push（支持 Last-Event-ID 重连补偿）
- GET /v1/events/pull     带游标拉取
- GET /v1/events          契约 §4.5 兼容（after 游标）
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from sgme.operations.events import (
    events_list as events_list_operation,
    events_pull as events_pull_operation,
    events_stream as events_stream_operation,
    http_payload as events_http_payload,
)
from sgme.server.app import require_agent_key, run_operation

logger = logging.getLogger("sgme.server.events")

router = APIRouter()


# ---------- GET /v1/events/pull ----------

@router.get("/v1/events/pull")
def events_pull(
    request: Request,
    subscriber_id: str = Query(..., description="订阅者标识"),
    last_signal_id: str | None = Query(None, description="可选游标，覆盖持久游标"),
    limit: int = Query(100, ge=1, le=1000),
    _: str = Depends(require_agent_key),
):
    """带持久游标拉取未消费事件。

    业务见 ``operations.events.events_pull``；本函数只做协议翻译：
    取依赖 → 调 operation → 投影为 HTTP 历史契约形态。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    data = run_operation(
        events_pull_operation,
        mem_conn,
        subscriber_id,
        last_signal_id,
        limit,
    )
    return events_http_payload(data)


# ---------- GET /v1/events（契约 §4.5 兼容：after 游标） ----------

@router.get("/v1/events")
def events_after(
    request: Request,
    after: str | None = Query(None, description="契约 §4.5：上次事件 id 游标"),
    limit: int = Query(100, ge=1, le=1000),
    _: str = Depends(require_agent_key),
):
    """契约 §4.5 兼容路径：GET /v1/events?after={last_event_id}。

    等价 events_pull（subscriber_id 固定为契约兼容订阅者）。
    业务见 ``operations.events.events_list``；本函数只做协议翻译。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn
    data = run_operation(events_list_operation, mem_conn, after, limit)
    return events_http_payload(data)


# ---------- GET /v1/events/stream ----------

def _sse_frames(stream: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    """协议翻译：操作层流项 → SSE 帧（帧格式与改造前逐字节一致）。

    - 事件项 {event_id, type, source, payload, ts} → id:/event:/data: 帧
    - keepalive 哨兵（KEEPALIVE_MARK）→ ": keepalive" 注释帧
    """
    async def _translate() -> AsyncIterator[str]:
        async for item in stream:
            if item.get("keepalive"):
                yield ": keepalive\n\n"
                continue
            data = {
                "event_id": item["event_id"],
                "type": item["type"],
                "source": item["source"],
                "payload": item["payload"],
                "ts": item["ts"],
            }
            yield (
                f"id: {item['event_id']}\n"
                f"event: {item['type']}\n"
                f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            )
    return _translate()


@router.get("/v1/events/stream")
async def event_stream(
    request: Request,
    subscriber_id: str = Query("sse-default", description="订阅者标识"),
    _: str = Depends(require_agent_key),
):
    """SSE push 端点。支持 Last-Event-ID 头重连补偿。

    业务（轮询间隔 / keepalive 周期 / 断连退出 / 游标补偿）见
    ``operations.events.events_stream``；本函数只做协议翻译：
    读 Last-Event-ID 头 → 调 operation 生成流 → 组 SSE 帧。
    响应头与改造前完全一致。
    """
    mem_conn: sqlite3.Connection = request.app.state.mem_conn

    # Last-Event-ID 重连补偿：读头（协议）→ 交 operations 在进入流前立即生效
    last_event_id = request.headers.get("Last-Event-ID")
    stream = events_stream_operation(
        mem_conn=mem_conn,
        subscriber_id=subscriber_id,
        last_event_id=last_event_id,
        is_disconnected=request.is_disconnected,
    )

    return StreamingResponse(
        _sse_frames(stream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
