"""operations/health.py：健康检查操作（v0.7 §7 operations 层**样板模块**）。

后续 append/inject/search/memory/refine/stats/config 八个模块照本模块的形状复制。

三段式结构（复制时保持）：
1. 常量/私有工具函数（本模块内聚，不外泄）
2. ``xxx(...) -> OperationResult`` 操作函数：显式接参，返回**协议无关的信息超集**
3. ``http_payload(data)`` / ``mcp_payload(data)`` 投影函数：把超集裁剪成各入口的历史契约形态

⚠️ 历史契约差异（v0.8 待统一，现在**不得**合并）
--------------------------------------------------
HTTP ``GET /v1/health`` 与 MCP ``health`` 工具的 refinement 字段形态**本就不同**：

- HTTP：**重组超集**——额外算 ``watermark_age_sec``，并把 engine 返回的顶层
  ``queue_depth`` / ``stalled`` / ``heartbeat_ok`` 提升进 refinement 子对象。
- MCP：**原始透传**——直接给 ``check_heartbeat()["refinement"]``，
  含 ``threshold_hours``，但**没有** watermark_age_sec / queue_depth / heartbeat_ok。

v0.7 的目标是抽取业务逻辑，**不是**统一 API 契约（用户硬约束：端点响应格式不变）。
因此本模块的 ``data`` 同时携带两种形态（``refinement`` 与 ``refinement_raw``），
由两个投影函数各自还原，保证改造前后两端输出**逐字节等价**。
统一为单一形态是 v0.8 的议题，届时删掉 ``refinement_raw`` 与 ``mcp_payload`` 即可。

依赖：只调 ``sgme.engine.health.check_heartbeat``（engine 是禁区，只读不改）。
副作用：``check_heartbeat`` 在心跳异常时会发布 ``anomaly_warn`` 信号——
这是既有可观测性行为，抽取后必须保留，不得"优化"掉。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from sgme.engine import health as engine_health
from sgme.operations.errors import OperationResult

# 版本号：原先硬编码在 routes_memory.health_check 与 mcp_server.health 两处，
# v0.7 收敛到此单点常量。取值与 sgme.__version__ 一致（两者的统一属 v0.8 清理项，
# 此处不直接引用 __version__，避免版本号变更静默改动 API 契约字段）。
SGME_VERSION: str = "1.0.0b1"


def watermark_age_sec(last_refined_at: str | None) -> int | None:
    """提炼水位年龄：最近一次 refined_at 距今秒数。

    口径与 v0.6 ``routes_memory.health_check`` 逐行一致：
    - 空值（None / 空串）→ None
    - ISO 解析失败（ValueError / TypeError）→ None，**不抛异常**（容错必须保留）

    ⚠️ 本函数是 health 与 stats 两个操作模块的**共用口径**——v0.6 时代
    ``routes_memory.health_check`` 与 ``routes_admin.admin_stats`` 各抄了一遍
    同样的十行，正是 operations 层要消灭的重复。故此处提升为公开函数，
    ``operations/stats.py`` 直接复用（同层平级 import，无环）。

    Args:
        last_refined_at: ISO 8601 时间戳字符串，可为 None。

    Returns:
        距今秒数（int），无法计算时为 None。
    """
    if not last_refined_at:
        return None
    try:
        t = datetime.fromisoformat(last_refined_at.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - t).total_seconds())
    except (ValueError, TypeError):
        return None


# 向后兼容别名：health 切片（commit d36fef1）以私有名发布，测试与既有调用方沿用。
_watermark_age_sec = watermark_age_sec


# ---------- 向量可用性（ST-22②：health 加 vector 可用性） ----------

def check_vector_availability(mem_conn: sqlite3.Connection) -> dict[str, Any]:
    """向量检索可用性探测：引擎（sqlite-vec / numpy 降级）+ 向量数据规模。

    新手体验加固（Backlog ST-22②）：``/v1/health`` 需告知调用方向量通路是否可用、
    以及库里已有多少向量（0 条 → ``/search`` 向量通路将回退纯 BM25）。

    口径：
    - engine：sqlite-vec 扩展可加载 → ``"sqlite-vec"``；否则 numpy 可用 →
      ``"numpy-fallback"``；两者皆不可用 → ``"unavailable"``
    - available = engine != "unavailable"（numpy 降级仍视为可用，reason 说明降级）
    - memory_vectors / scene_vectors：两表行数（表缺失按 0 计，不抛异常）
    - reason：仅在有降级 / 异常 / 无向量数据时给出中文说明，一切正常为 None

    ⚠️ 本函数**永不抛异常**（健康检查必须健壮）：任何探测失败 → available=False + 原因。
    ⚠️ 数据层例外：data/ 层无向量计数 DAO，且本任务不允许改 data/；
    此处仅做只读 COUNT 内省（无副作用），不承担任何查询逻辑。

    Args:
        mem_conn: memory.db 连接（memory_vectors / scene_vectors 所在库）。

    Returns:
        dict：available / engine / memory_vectors / scene_vectors / reason。
    """
    from sgme.data.search import vector as vector_mod

    try:
        # 引擎探测：与 /search 向量检索同一判定入口（try_load_vec_extension）
        vec_ext_ok = vector_mod.try_load_vec_extension(mem_conn)
        numpy_ok = True
        try:
            import numpy  # noqa: F401
        except Exception:
            numpy_ok = False

        if vec_ext_ok:
            engine = "sqlite-vec"
            reason: str | None = None
        elif numpy_ok:
            engine = "numpy-fallback"
            reason = "sqlite-vec 扩展不可用，已降级 numpy 余弦检索（功能可用，大数据量性能较低）"
        else:
            engine = "unavailable"
            reason = "向量引擎不可用：sqlite-vec 与 numpy 均加载失败"

        counts = _count_vector_rows(mem_conn)
        # 无向量数据 → 附加引导提示（新手可据此判断 /search 向量通路为何不生效）
        if engine != "unavailable" and counts["memory_vectors"] + counts["scene_vectors"] == 0:
            hint = "尚无向量数据：/search 向量通路将回退纯 BM25，可先沉淀记忆并触发提炼/场景生成"
            reason = f"{reason}；{hint}" if reason else hint

        return {
            "available": engine != "unavailable",
            "engine": engine,
            "memory_vectors": counts["memory_vectors"],
            "scene_vectors": counts["scene_vectors"],
            "reason": reason,
        }
    except Exception as e:  # 探测本身失败 → 不可用 + 原因（不向上抛）
        return {
            "available": False,
            "engine": "unavailable",
            "memory_vectors": 0,
            "scene_vectors": 0,
            "reason": f"向量可用性探测失败: {e}",
        }


def _count_vector_rows(mem_conn: sqlite3.Connection) -> dict[str, int]:
    """向量表行数统计（表缺失按 0 计；只读 COUNT，无副作用）。"""
    counts = {"memory_vectors": 0, "scene_vectors": 0}
    try:
        tables = {r["name"] for r in mem_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    except Exception:
        return counts
    for table in counts:
        if table not in tables:
            continue
        try:
            row = mem_conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            counts[table] = row["c"] if row else 0
        except Exception:
            counts[table] = 0
    return counts


def health(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
) -> OperationResult:
    """健康检查：LLM 可用性 + 提炼水位 + 队列深度 + 心跳。

    签名刻意**只收业务依赖**：operations 层不认识 ``request.app.state``
    或 mcp 的 ``_app_state``（那是入口层的协议细节），由入口层取出后显式传入。

    Args:
        mem_conn: memory.db 连接（anomaly_warn 信号落库用）。
        session_conn: session.db 连接（raw_files 队列/水位查询用）。
        cfg: 运行时配置（LLM 链路探测用）。

    Returns:
        OperationResult(ok=True)，``data`` 为协议无关信息超集：
        - status: 固定 "ok"
        - version: SGME 版本号
        - llm: engine 返回的 llm 探测结果（available/provider/model/error）
        - refinement: **HTTP 历史形态**（重组超集，含 watermark_age_sec）
        - refinement_raw: **MCP 历史形态**（engine 原始 refinement 子对象）

        本操作不返回失败态：``check_heartbeat`` 自身已吞掉 LLM 探测与信号发布异常，
        剩余异常（如 sqlite 故障）按 v0.6 行为**继续向上抛**，
        由入口层的全局异常处理器兜底——刻意不在此加 catch-all，避免改变错误响应形态。
    """
    heartbeat = engine_health.check_heartbeat(mem_conn, session_conn, cfg)

    raw_refinement: dict[str, Any] = heartbeat["refinement"]
    last_refined = raw_refinement.get("last_refined_at")

    data: dict[str, Any] = {
        "status": "ok",
        "version": SGME_VERSION,
        "llm": heartbeat["llm"],
        # —— ST-22②：向量可用性（引擎 + 数据规模；永不抛异常）——
        "vector": check_vector_availability(mem_conn),
        # —— HTTP 历史形态：字段顺序即 v0.6 响应体顺序，勿调整 ——
        "refinement": {
            "watermark_age_sec": watermark_age_sec(last_refined),
            "queue_depth": heartbeat["queue_depth"],
            "last_refined_at": last_refined,
            "stalled": heartbeat["stalled"],
            "stalled_hours": raw_refinement.get("stalled_hours"),
            "heartbeat_ok": heartbeat["heartbeat_ok"],
        },
        # —— MCP 历史形态：engine 原始透传，勿加工 ——
        "refinement_raw": raw_refinement,
    }
    return OperationResult.succeed(data)


def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP ``GET /v1/health`` 的历史契约形态（v0.6 逐字段等价 + ST-22② vector）。

    ⚠️ ST-22② 有意新增顶层 ``vector`` 字段（向后兼容：只增不改既有字段）；
    MCP 形态不受影响（MCP health 契约冻结，向量字段属另一任务的议题）。
    """
    return {
        "status": data["status"],
        "version": data["version"],
        "llm": data["llm"],
        "refinement": data["refinement"],
        "vector": data["vector"],
    }


def mcp_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 MCP ``health`` 工具的历史契约形态（v0.6 逐字段等价）。

    与 http_payload 的唯一差异是 refinement 取原始透传版——历史差异，v0.8 待统一。
    """
    return {
        "status": data["status"],
        "version": data["version"],
        "llm": data["llm"],
        "refinement": data["refinement_raw"],
    }
