"""operations/append.py：L0 捕获操作（v0.7 §7 operations 层，样板见 health.py）。

承接的入口（两处，行为必须一致）：
- HTTP ``POST /v1/append``（sgme/server/routes_memory.py::append_session）
- MCP ``append`` 工具（sgme/mcp_server.py，只传 4 参：session_key/started_at/content/source_type）

三段式结构说明：本模块只含「操作函数」一段。
- 第 1 段（常量/私有工具）：无——本操作没有内聚的私有逻辑。
- 第 2 段（``append_l0(...) -> OperationResult``）：显式接参，调 engine 收口。
- 第 3 段（http_payload/mcp_payload 投影）：**本操作不需要**——engine 返回的
  成功 dict 在 HTTP 与 MCP 两端形态本就一致（v0.6 时代两端就都直接透传
  append_l0 的返回），没有历史契约差异可投影，成功态原样透传即可。

职责（相对 v0.6 的两个入口壳）：
1. 参数补齐：MCP 只传 4 参，``ended_at``/``agent_id``/``metadata`` 由本模块补
   None 默认值，``source_type`` 默认 "session"（与 MCP 工具签名一致）。
2. 异常翻译：把 engine 的裸异常翻译成 OperationResult 失败态（与 v0.6 路由
   的 api_error 映射逐条对应）：
   - ``ValueError``（content 解析出 0 条消息）→ ``ERR_INVALID_ARGS``
   - ``FileNotFoundError``（raw 文件丢失）→ ``ERR_NOT_FOUND``
   - 其它 ``Exception``（写盘故障等）→ ``ERR_INTERNAL``（文案 "写 L0 文件失败: ..."）

幂等语义（继承 engine，调用方按返回的 data 理解）：
- 同 session_key + 同 started_at → 不重复写，返回既有 file_id，``idempotent=True``
- 同 session_key + 不同 started_at → 追加到既有文件，status 重置为 "new"，
  ``appended=True``
- 全新 session_key → 新建文件，status="new"

依赖：只调 ``sgme.engine.pipeline.append_l0``（engine 是禁区，只读不改）。
副作用：refine_on_append 开启时后台联动提炼（engine 内部行为，本模块不干预）。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from sgme.engine import pipeline as pipeline_mod
from sgme.operations.errors import (
    ERR_INTERNAL,
    ERR_INVALID_ARGS,
    ERR_NOT_FOUND,
    OperationResult,
)


def append_l0(
    session_key: str,
    started_at: str,
    content: str,
    source_type: str = "session",
    ended_at: str | None = None,
    agent_id: str | None = None,
    metadata: dict | None = None,
    agent_model: str | None = None,
    *,
    cfg: dict[str, Any],
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
) -> OperationResult:
    """L0 捕获：写 raw 文件 + raw_files 索引（engine.pipeline.append_l0 的统一操作包装）。

    参数与 engine 同名同序，仅多默认值：MCP 工具只传
    session_key/started_at/content/source_type 四参，其余由本函数补 None/"session"。
    ``cfg``/``mem_conn``/``session_conn`` 声明为 keyword-only：它们是依赖注入位，
    不允许被位置参数误吞（入口层本就按关键字传）。

    Args:
        session_key: 会话标识（幂等/追加的锚点之一）。
        started_at: 起始时间（ISO 8601，幂等/追加的锚点之二）。
        content: 消息块纯文本（``# {ISO} {role}`` 行首格式，见 raw/store 解析）。
        source_type: 来源类型（session/upload/external），默认 "session"。
        ended_at: 结束时间，可选。
        agent_id: 代理标识，可选。
        metadata: 附加元数据，可选。
        cfg: 运行时配置（refine_on_append 联动提炼开关等）。
        mem_conn: memory.db 连接。
        session_conn: session.db 连接（raw_files 索引）。

    Returns:
        OperationResult：
        - 成功（ok=True），``data`` 为 engine append_l0 的返回 dict 原样透传：
          - 新建：``{file_id, path, status: "new"}``
          - 幂等：``{file_id, path, status, idempotent: True}``
          - 追加：``{file_id, path, status: "new", appended: True}``
        - 失败（ok=False），错误码语义：
          - ``ERR_INVALID_ARGS``：content 解析出 0 条消息
          - ``ERR_NOT_FOUND``：raw 文件丢失
          - ``ERR_INTERNAL``：其它写盘异常
    """
    try:
        data = pipeline_mod.append_l0(
            session_key=session_key,
            started_at=started_at,
            content=content,
            source_type=source_type,
            ended_at=ended_at,
            agent_id=agent_id,
            metadata=metadata,
            agent_model=agent_model,
            cfg=cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
        )
        return OperationResult.succeed(data)
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    except FileNotFoundError as e:
        return OperationResult.fail(ERR_NOT_FOUND, str(e))
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"写 L0 文件失败: {e}")
