"""operations/memory_usage.py：记忆调用统计操作（T-251 数据层 / T-252 接线 / T-253 端点）。

三段式（照 ``operations/skill_usage.py`` 样板；本模块响应即最终形态，单入口无需协议投影）。

承接的入口
----------
- 检索链路（``operations/search.py`` memory 层）→ ``record_memory_usage(source="search")``
- 注入链路（``operations/inject.py`` 两分支）→ ``record_memory_usage(source="inject")``
- HTTP ``GET /v1/admin/memories/usage``（管理员 Key）→ ``query_memory_usage`` /
  ``query_unused_memories``

口径与边界
----------
- 双源：``search`` = 检索命中（memory 层结果集）、``inject`` = 画像注入
  （``section_results`` 即注入集，T-203 G1 在案）；跨 section 去重由 DAO 兜底。
- A2 政策修订（2026-10-10 用户批）：**仅限本表**——检索链路可写
  ``memory_usage_daily``；``memory_stats`` / ``last_recalled_at`` 链路照旧禁止。
- caller 值域对齐 api_usage 中间件口径：agent_id / default / anonymous /
  ``unknown``（未解析出请求方时回落 ``unknown``，不发明新值）。
- ``record_memory_usage`` 全静默（best-effort）：统计是旁路，任何失败（含 DAO 层
  ValueError 等参数错误）都不得影响主链路。
"""

from __future__ import annotations

import logging
import sqlite3

from sgme.data import memory_usage_dao

logger = logging.getLogger("sgme.operations.memory_usage")

#: 来源白名单（与 data 层一致）
SOURCES = memory_usage_dao.SOURCES

MAX_DAYS = 400
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
DEFAULT_UNUSED_LIMIT = 500

#: 排序白名单：hits=次数降序 / last_ts=最近调用降序
ORDERS = ("hits", "last_ts")


def record_memory_usage(
    conn: sqlite3.Connection,
    memory_ids: list[str],
    source: str,
    caller: str | None,
) -> None:
    """记录一批记忆调用（source=search|inject；全静默——统计是旁路，失败不影响主链路）。

    caller 未解析出请求方（None/空）→ 回落 ``unknown``（值域对齐 api_usage 口径）。
    """
    try:
        memory_usage_dao.record_usage(conn, memory_ids, source, caller or "unknown")
    except Exception as e:  # noqa: BLE001 —— best-effort：任何失败不得影响主链路
        logger.warning(
            "记忆调用统计写入失败（已忽略，不影响主链路）：source=%s, err=%s",
            source,
            e,
        )
