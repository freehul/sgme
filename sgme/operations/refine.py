"""operations/refine.py：提炼触发操作（v0.7 §7 operations 层，同步/异步双操作模块）。

三段式结构（照抄 health.py 样板）：
1. 常量/私有工具函数（本模块内聚，不外泄）
2. ``xxx(...) -> OperationResult`` 操作函数：显式接参，返回**协议无关的信息超集**
3. ``http_payload(data)`` / ``mcp_payload(data)`` 投影函数：把超集裁剪成各入口的历史契约形态

承接的入口
----------
========================================  ========================================
入口                                       操作
========================================  ========================================
HTTP ``POST /v1/admin/refine/trigger``     ``refine_trigger`` + ``http_payload``
HTTP ``POST /v1/admin/refine/trigger_async``  ``refine_trigger_async`` + ``http_payload``
MCP  ``refine_trigger``                    ``refine_trigger`` / ``refine_trigger_async``
                                           + ``mcp_payload``（按 async_mode 分流）
MCP  ``refine_batch``                      ``refine_batch``（无投影，data 即响应）
MCP  ``refine_status``                     ``refine_status``（无投影，data 即响应）
========================================  ========================================

同步/异步语义差异
----------------
- 同步（``refine_trigger``）：**阻塞执行**，可能耗时数分钟（真实 LLM 分块），
  返回完整结果（triggered=file/batch，含 status / memories_count / l15 /
  new_last_refined_seq / anomaly_warn / error / prompt_versions）。
- 异步（``refine_trigger_async``）：**后台线程立即返回**排队语义
  （triggered=async / status=queued / note），结果异步落库，
  可用 ``/v1/health`` 的 refinement 水位观察进度；后台执行体
  ``engine.pipeline.async_refine_worker`` 逐文件容错、异常不抛出
  （失败由 SGME 批扫兜底：status=new 文件会被下次 trigger 拾起）。
- MCP 入口的 ``async_mode`` 参数即这两个操作的分流开关：
  ``async_mode=True`` → 调 ``refine_trigger_async``；``False`` → 调 ``refine_trigger``。
  （HTTP 侧没有 async_mode 参数，两个端点各对应一个操作。）

⚠️ 历史契约差异（v0.8 待统一，现在**不得**合并）
--------------------------------------------------
HTTP 与 MCP 的 refine_trigger 响应形态**本就不同**，v0.7 抽取时不得强行统一：

- HTTP 同步（file）：``{triggered, file_id, status, memories_count,
  new_last_refined_seq, anomaly_warn, error, l15, prompt_versions}`` —— 信息超集
- HTTP 同步（batch）：``{triggered, processed, total_memories, results: [...]}``
- HTTP 异步：``{triggered, file_id, status, note}``
- MCP 同步（file）：``{triggered, status, memories_count}``（子集，无 l15 等）
- MCP 同步（batch）：``{triggered, processed}``（子集）
- MCP 异步：``{triggered, status}``（子集）

操作函数的 ``data`` 一律取 **HTTP 形态超集**（键序即 v0.6 响应序），
``http_payload`` 恒等返回，``mcp_payload`` 按 triggered 裁剪为 MCP 子集。

注：v0.6 路由 ``/v1/admin/refine/trigger_async`` 的 docstring 写「立即返回 202」，
但实现未设 ``status_code=202``，实际 HTTP 状态是 200。该历史现状保持不变
（状态码是入口层协议细节，v0.8 统一时再定）。

异常翻译（以 v0.6 路由行为为基准，v0.7 显式化）
------------------------------------------------
1. ``limit`` 非法（None / 非正整数）→ 抛 ``InvalidArgs``（ERR_INVALID_ARGS）。
   v0.6 路由只靠 FastAPI 保证 int 类型、未校验正负，v0.7 操作层补齐显式校验。
2. ``file_id`` 不存在 → 返回 ``OperationResult.fail(ERR_NOT_FOUND)``，
   文案沿用 engine ``raw_files 表无记录: {file_id}``。v0.6 路由对该场景返回
   200 + ``status:"error"``；v0.7 按 §7.4 规范把「资源不存在」显式化为
   ERR_NOT_FOUND（父任务拍板：ERR_NOT_FOUND 或现有行为，取前者）。
   若接线时须保留 200 形态，由入口层投影还原，操作层语义不再含糊。
3. 穿透 pipeline 的非预期异常 → ``result_from_exception`` 收敛为
   ERR_INTERNAL 失败结果。与 health（探测类、异常原样上抛）不同：
   refine 是**写操作**，失败必须显式可编程处理（v0.7 决策，见测试⑤）。
4. refine_file 的**业务失败**（``status="error"``，如 L0 解析失败）**不吞**——
   照 v0.6 作为响应字段透传（status/error 键），因为那是「提炼失败」的
   业务结果而非崩溃，200 响应形态保持不变。

依赖：只调 ``sgme.engine.pipeline``（提炼编排唯一出口）+ 
``sgme.data.session_dao``（存在性预检）。engine 是只读禁区，不改。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any

from sgme.engine import pipeline as pipeline_mod
from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs, OperationResult, result_from_exception
from sgme.operations.health import watermark_age_sec
from sgme.data import refine_dao, session_dao, stats_dao

logger = logging.getLogger("sgme.operations.refine")

# 异步响应的固定文案：v0.6 路由内联字符串，v0.7 收敛到此单点常量。
ASYNC_QUEUED_NOTE: str = "后台线程执行，结果异步落库；可用 /v1/health refinement 水位观察进度"


def _validate_limit(limit: int) -> None:
    """limit 参数校验（两操作共用）。

    v0.6 路由由 FastAPI/Pydantic 保证 int 类型，但未校验正负；
    v0.7 操作层显式化：非正整数是参数错误（入口层映射 400/422）。
    """
    if limit is None or limit <= 0:
        raise InvalidArgs(f"limit 必须为正整数，收到: {limit!r}")


def _not_found(file_id: str) -> OperationResult:
    """构造「raw_files 无记录」失败结果。

    文案沿用 engine/refine.py::refine_file 的既有错误串
    （``raw_files 表无记录: {file_id}``），保证日志/响应可追溯一致。
    """
    return OperationResult.fail(ERR_NOT_FOUND, f"raw_files 表无记录: {file_id}")


def refine_trigger(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    file_id: str | None = None,
    limit: int = 100,
) -> OperationResult:
    """同步触发提炼：指定 file_id 单文件，或扫 status=new 批量。

    签名刻意**只收业务依赖**：operations 层不认识 ``request.app.state``
    或 mcp 的 ``_app_state``，由入口层取出后显式传入。

    Args:
        mem_conn: memory.db 连接。
        session_conn: session.db 连接（raw_files 队列/游标）。
        cfg: 运行时配置（LLM 链路与提炼参数）。
        file_id: 指定单文件；None / 空串 → 批量（v0.6 路由
            ``if payload.file_id:`` 的假值语义，空串不得改为报错）。
        limit: 批量上限，必须为正整数。

    Returns:
        - 成功：``OperationResult(ok=True)``，data 为 HTTP 形态超集
          （``triggered`` = "file" / "batch"，含 status / memories_count /
          l15 / new_last_refined_seq / anomaly_warn / error / prompt_versions）。
        - file_id 不存在：``ok=False``，error_code=ERR_NOT_FOUND。
        - limit 非法：抛 ``InvalidArgs``（ERR_INVALID_ARGS）。
        - pipeline 非预期异常：``ok=False``，error_code=ERR_INTERNAL。
    """
    _validate_limit(limit)
    try:
        if file_id:
            # 存在性预检（v0.7 规范：可预期业务失败先于业务调用判定，
            # 与 operations/memory.py 的「先 get 判存在」同一模式）
            rf = session_dao.get_raw_file(session_conn, file_id)
            if rf is None:
                return _not_found(file_id)
            result, l15_stats = pipeline_mod.refine_one(
                file_id, mem_conn, session_conn, cfg,
            )
            # —— HTTP 历史形态：键序即 v0.6 响应体顺序，勿调整 ——
            return OperationResult.succeed({
                "triggered": "file",
                "file_id": file_id,
                "status": result.status,
                "memories_count": len(result.memories),
                "new_last_refined_seq": result.new_last_refined_seq,
                "anomaly_warn": result.anomaly_warn,
                "error": result.error,
                "l15": l15_stats,
                "prompt_versions": result.prompt_versions,
            })

        pairs = pipeline_mod.refine_many(limit, mem_conn, session_conn, cfg)
        return OperationResult.succeed({
            "triggered": "batch",
            "processed": len(pairs),
            "total_memories": sum(len(r.memories) for r, _ in pairs),
            "results": [
                {
                    "file_id": r.file_id,
                    "status": r.status,
                    "memories_count": len(r.memories),
                    "new_last_refined_seq": r.new_last_refined_seq,
                    "anomaly_warn": r.anomaly_warn,
                    "error": r.error,
                    "l15": l15_stats,
                    "prompt_versions": r.prompt_versions,
                }
                for r, l15_stats in pairs
            ],
        })
    except Exception as e:
        # 非预期异常 → ERR_INTERNAL（errors.result_from_exception 统一收敛）。
        # 与 health.py 的「原样上抛」不同：refine 是写操作，
        # 失败必须显式可编程处理，见模块 docstring「异常翻译」第 3 条。
        return result_from_exception(e)


def refine_trigger_async(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    file_id: str | None = None,
    limit: int = 100,
) -> OperationResult:
    """异步触发提炼：后台线程执行，立即返回排队语义。

    后台执行体 ``engine.pipeline.async_refine_worker`` 逐文件容错、异常不抛出
    （由批扫兜底），故本操作的可预期失败只有参数非法与线程启动失败两种。

    Args:
        mem_conn: memory.db 连接（后台线程使用）。
        session_conn: session.db 连接（后台线程使用）。
        cfg: 运行时配置（后台线程使用）。
        file_id: 指定单文件；None / 空串 → 批量（``file_id or "batch"`` 假值语义）。
        limit: 批量上限，必须为正整数。

    Returns:
        - 成功：``OperationResult(ok=True)``，data 为
          ``{"triggered": "async", "file_id", "status": "queued", "note"}``
          （键序即 v0.6 响应序）。
        - limit 非法：抛 ``InvalidArgs``（ERR_INVALID_ARGS）。
        - 线程启动失败：``ok=False``，error_code=ERR_INTERNAL。
    """
    _validate_limit(limit)
    try:
        threading.Thread(
            target=pipeline_mod.async_refine_worker,
            args=(file_id, limit, mem_conn, session_conn, cfg),
            daemon=True,
        ).start()
    except Exception as e:
        # 线程启动失败（如系统线程耗尽）→ ERR_INTERNAL，不吞。
        return result_from_exception(e)
    return OperationResult.succeed({
        "triggered": "async",
        "file_id": file_id or "batch",
        "status": "queued",
        "note": ASYNC_QUEUED_NOTE,
    })


def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP ``POST /v1/admin/refine/trigger(_async)`` 的历史契约形态。

    data 即 HTTP 响应体（v0.7 以 HTTP 形态为信息超集、MCP 才是子集），
    故恒等返回。保留本函数是为三段式样板对齐 + 接线时入口层有单一投影入口。
    """
    return data


def mcp_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 MCP ``refine_trigger`` 工具的历史契约形态（v0.6 逐字段等价）。

    按 ``triggered`` 分流裁剪为 MCP 子集（无 l15 / new_last_refined_seq /
    anomaly_warn / prompt_versions / note 等）：
    - "async" → ``{"triggered", "status"}``
    - "file"  → ``{"triggered", "status", "memories_count"}``
    - "batch" → ``{"triggered", "processed"}``

    历史差异，v0.8 待统一——现在不得把子集字段合并进超集。
    """
    t = data["triggered"]
    if t == "async":
        return {"triggered": "async", "status": data["status"]}
    if t == "file":
        return {
            "triggered": "file",
            "status": data["status"],
            "memories_count": data["memories_count"],
        }
    return {"triggered": "batch", "processed": data["processed"]}


# ---------- 操作 3：批量文件提炼（ST-22⑥，MCP refine_batch） ----------

# 批量 results 内单文件项的业务键（与 refine_trigger 批量 results 项对齐，
# 便于两端共享同一消费逻辑）。
_BATCH_ITEM_KEYS: tuple[str, ...] = (
    "file_id", "status", "memories_count", "new_last_refined_seq",
    "anomaly_warn", "error", "l15", "prompt_versions",
)


def _batch_item(
    file_id: str, result: Any, l15_stats: dict[str, Any] | None,
) -> dict[str, Any]:
    """构造批量 results 单文件项（键序即 _BATCH_ITEM_KEYS）。"""
    return {
        "file_id": file_id,
        "status": result.status,
        "memories_count": len(result.memories),
        "new_last_refined_seq": result.new_last_refined_seq,
        "anomaly_warn": result.anomaly_warn,
        "error": result.error,
        "l15": l15_stats,
        "prompt_versions": result.prompt_versions,
    }


def _async_batch_worker(
    file_ids: list[str],
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
) -> None:
    """批量提炼后台执行体（显式文件列表版，线程内运行，异常不抛出）。

    与 ``engine.pipeline.async_refine_worker`` 同款「逐文件容错」：单文件失败
    只记日志、继续下一文件，由批扫兜底。engine 的 worker 只认单文件/全量扫描
    两种范围，显式文件列表由本 worker 处理（engine 是只读禁区，不外扩）。
    """
    for file_id in file_ids:
        try:
            result, _ = pipeline_mod.refine_one(file_id, mem_conn, session_conn, cfg)
            logger.info("async refine_batch file=%s status=%s", file_id, result.status)
        except Exception as e:
            logger.warning("async refine_batch 文件 %s 失败（继续下一文件）: %s", file_id, e)


def _validate_file_ids(
    file_ids: list[str],
    session_conn: sqlite3.Connection,
) -> OperationResult | None:
    """显式文件列表的存在性预检（与 refine_trigger 单文件同一模式）。

    Args:
        file_ids: 非空文件 id 列表。
        session_conn: session.db 连接（raw_files 查询用）。

    Returns:
        None 表示全部存在；否则返回失败结果（调用方直接返回该结果）。
    """
    for file_id in file_ids:
        rf = session_dao.get_raw_file(session_conn, file_id)
        if rf is None:
            return _not_found(file_id)
    return None


def refine_batch(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    file_ids: list[str] | None = None,
    limit: int = 100,
    async_mode: bool = True,
) -> OperationResult:
    """批量文件提炼：显式文件列表，或扫 status=new 全部（MCP refine_batch 专用）。

    与 ``refine_trigger`` 的分工：refine_trigger 是 v0.6 既有契约（单文件/扫全部），
    refine_batch 面向「一次给一批文件」的 agent 场景，范围语义更明确：
    - ``file_ids`` 给显式列表 → 只处理列表内文件（同步逐文件容错 / 异步排队即返）；
    - ``file_ids=None`` → 扫 status=new 全部（上限 ``limit``），与 refine_trigger
      批量语义一致（同步走 pipeline.refine_many，异步走 engine 全量 worker）。

    Args:
        mem_conn: memory.db 连接。
        session_conn: session.db 连接（raw_files 队列/存在性预检）。
        cfg: 运行时配置（LLM 链路与提炼参数）。
        file_ids: 显式文件列表；None → 扫 status=new 全部。空列表是参数错误
            （不传参数才是「全部」，两者语义不可混）。
        limit: 扫全部时的批量上限，必须为正整数（显式列表模式不使用）。
        async_mode: True → 后台线程执行、立即返回排队语义；False → 同步执行。

    Returns:
        - 成功：``OperationResult(ok=True)``。异步 data 为排队语义
          （triggered/status/scope/file_ids/limit/note）；同步 data 为
          ``{"triggered": "batch", "requested", "processed", "total_memories",
          "results": [...]}``。
        - file_ids 含不存在 id：``ok=False``，error_code=ERR_NOT_FOUND。
        - limit 非法 / file_ids 空列表：抛 ``InvalidArgs``。
        - pipeline 非预期异常：``ok=False``，error_code=ERR_INTERNAL。
    """
    _validate_limit(limit)
    if file_ids is not None and not file_ids:
        raise InvalidArgs("file_ids 为空列表（不传则以 status=new 全部为范围）")
    if file_ids is not None:
        bad = _validate_file_ids(file_ids, session_conn)
        if bad is not None:
            return bad

    if async_mode:
        try:
            if file_ids is not None:
                threading.Thread(
                    target=_async_batch_worker,
                    args=(file_ids, mem_conn, session_conn, cfg),
                    daemon=True,
                ).start()
            else:
                threading.Thread(
                    target=pipeline_mod.async_refine_worker,
                    args=(None, limit, mem_conn, session_conn, cfg),
                    daemon=True,
                ).start()
        except Exception as e:
            # 线程启动失败（如系统线程耗尽）→ ERR_INTERNAL，不吞。
            return result_from_exception(e)
        return OperationResult.succeed({
            "triggered": "async",
            "status": "queued",
            "scope": "files" if file_ids is not None else "pending",
            "file_ids": list(file_ids) if file_ids is not None else None,
            "limit": limit,
            "note": ASYNC_QUEUED_NOTE,
        })

    # —— 同步：显式列表逐文件容错（单文件失败不拖垮整批）——
    try:
        if file_ids is not None:
            results: list[dict[str, Any]] = []
            for file_id in file_ids:
                try:
                    r, l15_stats = pipeline_mod.refine_one(file_id, mem_conn, session_conn, cfg)
                    results.append(_batch_item(file_id, r, l15_stats))
                except Exception as e:
                    # 单文件非预期异常：记录 error 项、继续下一文件（与
                    # _async_batch_worker 同一容错取向）。
                    results.append({
                        "file_id": file_id,
                        "status": "error",
                        "memories_count": 0,
                        "new_last_refined_seq": None,
                        "anomaly_warn": None,
                        "error": str(e),
                        "l15": None,
                        "prompt_versions": {},
                    })
            return OperationResult.succeed({
                "triggered": "batch",
                "requested": len(file_ids),
                "processed": len(results),
                "total_memories": sum(
                    r["memories_count"] for r in results if r["status"] != "error"
                ),
                "results": results,
            })

        pairs = pipeline_mod.refine_many(limit, mem_conn, session_conn, cfg)
        return OperationResult.succeed({
            "triggered": "batch",
            "requested": None,
            "processed": len(pairs),
            "total_memories": sum(len(r.memories) for r, _ in pairs),
            "results": [_batch_item(r.file_id, r, l15_stats) for r, l15_stats in pairs],
        })
    except Exception as e:
        # 非预期异常 → ERR_INTERNAL（与 refine_trigger 同一收敛策略）。
        return result_from_exception(e)


# ---------- 操作 4：提炼进度状态（ST-22⑥，MCP refine_status） ----------

def refine_status(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
) -> OperationResult:
    """提炼进度：待提炼/已完成/失败计数 + 水位 + 最近失败（MCP refine_status 专用）。

    数据来源（全部经 data 层 DAO，不写裸 SQL）：
    - pending / completed / failed / total / last_refined_at：
      ``stats_dao.raw_files_summary``（raw_files 状态计数 + 提炼水位）。
    - watermark_age_sec：``operations.health.watermark_age_sec``（health/stats
      两个操作模块的共用口径）。
    - last_failure：``refine_dao.list_runs_page(status='error', limit=1)``
      —— refine_runs 按 ``started_at DESC`` 排序，取最近一条失败提炼记录
      （含 file_id/stage/error 文案）。引擎没有独立任务表，raw_files 状态
      与 refine_runs 失败记录是仅有的持久化进度来源。

    Args:
        mem_conn: memory.db 连接（refine_runs 查询用）。
        session_conn: session.db 连接（raw_files 状态/水位查询用）。

    Returns:
        ``OperationResult(ok=True)``，data 为
        ``{"pending", "completed", "failed", "total", "watermark",
        "last_failure"}``。本操作无失败态（查询类，异常按 v0.6 行为上抛）。
    """
    summary = stats_dao.raw_files_summary(session_conn)
    error_runs, _ = refine_dao.list_runs_page(mem_conn, status="error", limit=1)
    last_failure: dict[str, Any] | None = None
    if error_runs:
        r = error_runs[0]
        last_failure = {
            "file_id": r["file_id"],
            "stage": r["stage"],
            "started_at": r["started_at"],
            "error": r["error"],
        }
    return OperationResult.succeed({
        "pending": summary["new"],
        "completed": summary["refined"],
        "failed": summary["error"],
        "total": summary["total"],
        "watermark": {
            "last_refined_at": summary["last_refined_at"],
            "watermark_age_sec": watermark_age_sec(summary["last_refined_at"]),
        },
        "last_failure": last_failure,
    })
