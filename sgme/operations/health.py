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

依赖：调 ``sgme.engine.health.check_heartbeat``（engine 是禁区，只读不改）
+ ``sgme.data.stats_dao``（向量行数统计，T-9 收口：data 是唯一数据库操作层）。
副作用：``check_heartbeat`` 在心跳异常时会发布 ``anomaly_warn`` 信号——
这是既有可观测性行为，抽取后必须保留，不得"优化"掉。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from sgme.data import stats_dao
from sgme.engine import health as engine_health
from sgme.llm.provider import make_client
from sgme.operations.errors import OperationResult
from sgme.operations.llm import detect_missing_model_keys, model_keys_notice

logger = logging.getLogger("sgme.operations.health")

# 版本号：原先硬编码在 routes_memory.health_check 与 mcp_server.health 两处，
# v0.7 收敛到此单点常量。取值与 sgme.__version__ 一致（两者的统一属 v0.8 清理项，
# 此处不直接引用 __version__，避免版本号变更静默改动 API 契约字段）。
SGME_VERSION: str = "1.0.0b4"


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


# ---------- 向量模型连通性（T-53 2026-08-18：health 加模型探测 + 失效信号） ----------

_VECTOR_PROBE_TIMEOUT_S = 5.0
# 健康探测用极短输入（免费模型零费用；仅连通性验证，不产生语义向量用途）
_VECTOR_PROBE_INPUT = "."


def check_vector_model_connectivity(
    cfg: dict,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """向量模型连通性探测：调 search.vector 的 embeddings 端点一次。

    - 输入 "."（极短），5s 超时，Bearer key（api_key_env 引用，铁律 #10 禁明文）
    - httpx 必须 trust_env=False（防代理劫持，make_client 保证）
    - **永不抛异常**（健康检查必须健壮）：任何失败 → available=False + error
    - 返回 available / provider / model / latency_ms / error

    与 check_vector_availability 的分工：那是「引擎 + 数据规模」（本地无网络）；
    本函数是「模型 API 连通性」（云端端点可达、key 有效）。两者互补。
    """
    vec = (cfg.get("search") or {}).get("vector") or {}
    provider = str(vec.get("provider", ""))
    model = str(vec.get("model", ""))
    base_url = (vec.get("base_url") or "").rstrip("/")
    api_key_env = vec.get("api_key_env") or ""
    if not base_url or not model:
        return {
            "available": False, "provider": provider, "model": model,
            "latency_ms": None, "error": "向量端点未配置（base_url/model 缺失）",
        }
    headers = None
    key = os.environ.get(api_key_env) if api_key_env else None
    if key:
        headers = {"Authorization": f"Bearer {key}"}
    own_client = client is None
    cli = client or make_client(timeout_s=_VECTOR_PROBE_TIMEOUT_S)
    t0 = time.monotonic()
    try:
        resp = cli.post(
            f"{base_url}/embeddings",
            json={"model": model, "input": _VECTOR_PROBE_INPUT},
            headers=headers,
            timeout=_VECTOR_PROBE_TIMEOUT_S,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 200:
            return {
                "available": True, "provider": provider, "model": model,
                "latency_ms": latency_ms, "error": None,
            }
        return {
            "available": False, "provider": provider, "model": model,
            "latency_ms": latency_ms, "error": f"HTTP {resp.status_code}",
        }
    except httpx.HTTPError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "available": False, "provider": provider, "model": model,
            "latency_ms": latency_ms, "error": str(e)[:200],
        }
    finally:
        if own_client:
            cli.close()


def _publish_vector_signal(
    mem_conn: sqlite3.Connection,
    conn_check: dict[str, Any],
) -> None:
    """向量模型失效 → anomaly_warn 信号（接入 agent 经 SSE/拉取可见并提醒用户）。

    - 复用 signal.engine.publish（既有的 anomaly_warn 通道，SSE/pull 消费端零改动）
    - suppress_hint 由 publish 内部处理（同源同类型 30 分钟重复附 hint）
    - 发布失败仅日志，不抛异常（与 check_heartbeat 的 anomaly_warn 同语义）
    """
    try:
        from sgme.signal import engine as signal_engine
        signal_engine.publish(
            event_type="anomaly_warn",
            source="vector",
            payload={
                "component": "vector_model",
                "provider": conn_check.get("provider"),
                "model": conn_check.get("model"),
                "error": conn_check.get("error"),
                "hint": "向量模型不可用：/search 向量通路将回退纯 BM25。"
                        "接入 agent 请提醒用户检查 SILICONFLOW_API_KEY / 硅基流动账户状态，"
                        "申请流程见 docs/guide/免费模型Key申请指南.md",
            },
            mem_conn=mem_conn,
        )
    except Exception as e:  # noqa: BLE001 —— 信号发布必须健壮，禁止上抛
        logger.warning("向量 anomaly_warn 发布失败（不阻塞）: %s", e)


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
    ⚠️ 向量行数统计走 ``data/stats_dao.py::count_vector_rows``（T-9 收口：
    data 是唯一数据库操作层，operations 层零 SQL）。

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

        counts = stats_dao.count_vector_rows(mem_conn)
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


def _vector_block(mem_conn: sqlite3.Connection, cfg: dict) -> dict[str, Any]:
    """向量块组装：引擎可用性 + 模型连通性两层；连通失败写日志 + 发信号。"""
    vec_avail = check_vector_availability(mem_conn)
    conn_check = check_vector_model_connectivity(cfg)
    if not conn_check["available"]:
        # 未配置（base_url/model 缺失）不算「失效」——Key 缺失引导（model_config）已覆盖，
        # 不发 anomaly_warn 避免噪音；仅配置存在但探测失败才告警。
        unconfigured = "未配置" in (conn_check.get("error") or "")
        logger.warning(
            "向量模型%s: %s (%s) latency=%s",
            "未配置" if unconfigured else "不可用",
            conn_check.get("model"), conn_check.get("error"),
            conn_check.get("latency_ms"),
        )
        if not unconfigured:
            _publish_vector_signal(mem_conn, conn_check)
    return {**vec_avail, "connectivity": conn_check}


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
        "vector": _vector_block(mem_conn, cfg),
        # —— T-53：模型 Key 缺失引导（免费托底新用户；只增不改既有字段）——
        "model_config": {
            "missing_keys": detect_missing_model_keys(cfg),
            "notice": model_keys_notice(cfg),
        },
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
        "model_config": data["model_config"],
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
