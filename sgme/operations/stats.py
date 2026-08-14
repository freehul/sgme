"""operations/stats.py：统计查询操作（v0.7 §7）。

三段式结构（照抄 health.py 样板）：
1. 常量/私有工具
2. ``stats(...) -> OperationResult``：返回协议无关的信息超集
3. ``http_payload`` / ``mcp_payload`` 投影函数

承接的入口
----------
- HTTP ``GET /v1/admin/stats``（管理员 Key）
- MCP  ``stats`` 工具

⚠️ 历史契约差异（v0.8 待统一，现在**不得**合并）
--------------------------------------------------
两端的统计响应**键序不同、字段集不同**：

============  ======================================================  ===============
入口           顶层键序                                                 refinement 形态
============  ======================================================  ===============
HTTP          memories / raw_files / dimension_distribution /          三键：watermark_age_sec
              refinement / **agents**                                  + last_refined_at + queue_depth
MCP           memories / **dimension_distribution** / raw_files /      单键：last_refined_at
              refinement（无 agents）
============  ======================================================  ===============

注意 HTTP 与 MCP 的第 2、3 个顶层键**互换**——这不是笔误，是 v0.6 两处独立
实现的真实差异。JSON 对象键序对某些下游（快照测试、diff 工具）可见，
故用两个投影函数分别还原，操作函数本身不做分流。

``agents`` 字段的依赖注入
-------------------------
HTTP 响应里的 ``agents`` 来自入口层的 ``AgentKeyStore``（鉴权设施，
**不是**数据层）。operations 不认识 AgentKeyStore，故由入口层显式传入原始
记录列表；本模块只负责裁剪为 ``{agent_id, role}``——顺带保证
``list_agents()`` 里的**明文 Key 绝不进入响应**（v0.6 亦然，此处固化该保证）。

依赖：统计查询一律走 ``sgme.data.stats_dao``（AGENTS.md 约束：统计 SQL
唯一出口）；水位年龄口径复用 ``operations.health.watermark_age_sec``
（同层平级复用，消除 v0.6 时代 routes_memory / routes_admin 的两份抄写）。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from sgme.operations.errors import OperationResult
from sgme.operations.health import watermark_age_sec
from sgme.data import stats_dao


def _public_agents(agents: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """裁剪 Agent 记录为对外形态 ``{agent_id, role}``。

    🔴 入参可能含明文 API Key（``AgentKeyStore.list_agents()`` 的返回值），
    本函数是它进入响应体前的**唯一收口**——只取两个字段，其余一律丢弃。

    Args:
        agents: 原始 Agent 记录列表；None 表示入口未提供（MCP 场景）。

    Returns:
        裁剪后的列表；入参为 None 时返回空列表。
    """
    if not agents:
        return []
    return [{"agent_id": a["agent_id"], "role": a["role"]} for a in agents]


def stats(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    *,
    agents: list[dict[str, Any]] | None = None,
) -> OperationResult:
    """统计：记忆/原始层计数、维度分布、提炼水位、注册 Agent。

    Args:
        mem_conn: memory.db 连接（记忆计数 + 维度分布）。
        session_conn: session.db 连接（raw_files 计数 + 水位）。
        agents: 入口层提供的 Agent 记录列表（HTTP 传 ``key_store.list_agents()``；
            MCP 不传）。

    Returns:
        ``OperationResult(ok=True)``，data 为协议无关信息超集：
        - memories / raw_files / dimension_distribution：两端共用
        - refinement：**HTTP 历史形态**（含 watermark_age_sec / queue_depth）
        - refinement_raw：**MCP 历史形态**（只有 last_refined_at）
        - agents：裁剪后的 Agent 列表（MCP 投影会丢弃）

        本操作不返回失败态；sqlite 故障等非预期异常**原样上抛**
        （不加 catch-all），由入口层全局异常处理器兜底——与 v0.6 一致。
    """
    mem_summary = stats_dao.memory_summary(mem_conn)
    raw_summary = stats_dao.raw_files_summary(session_conn)
    dimension_dist = stats_dao.dimension_distribution(mem_conn)
    last_refined = raw_summary["last_refined_at"]

    data: dict[str, Any] = {
        "memories": {
            "total": mem_summary["total"],
            "archived": mem_summary["archived"],
        },
        "raw_files": {
            "total": raw_summary["total"],
            "new": raw_summary["new"],
            "refined": raw_summary["refined"],
            "error": raw_summary["error"],
            "archived": raw_summary["archived"],
        },
        "dimension_distribution": dimension_dist,
        # —— HTTP 历史形态：字段顺序即 v0.6 响应体顺序，勿调整 ——
        "refinement": {
            "watermark_age_sec": watermark_age_sec(last_refined),
            "last_refined_at": last_refined,
            "queue_depth": raw_summary["new"],
        },
        # —— MCP 历史形态：只暴露 last_refined_at ——
        "refinement_raw": {"last_refined_at": last_refined},
        "agents": _public_agents(agents),
    }
    return OperationResult.succeed(data)


def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP ``GET /v1/admin/stats`` 的历史契约形态（v0.6 逐字段等价）。"""
    return {
        "memories": data["memories"],
        "raw_files": data["raw_files"],
        "dimension_distribution": data["dimension_distribution"],
        "refinement": data["refinement"],
        "agents": data["agents"],
    }


def mcp_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 MCP ``stats`` 工具的历史契约形态（v0.6 逐字段等价）。

    与 http_payload 的差异：顶层第 2/3 键互换、refinement 取单键版、无 agents。
    """
    return {
        "memories": data["memories"],
        "dimension_distribution": data["dimension_distribution"],
        "raw_files": data["raw_files"],
        "refinement": data["refinement_raw"],
    }
