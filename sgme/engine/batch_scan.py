"""engine/batch_scan.py：Batch 兜底自动提炼（ST-23② 保底型）——常驻定时器扫 status='new' 批量提炼。

定位（Backlog ST-23② 保底型适配）：服务端兜底，不依赖 agent 自觉的自动提炼。
hooks 型适配器（Hermes/Reasonix）正常走会话结束提炼；但会话异常退出
（崩溃/杀进程/断网）时 raw_files 表 status='new' 的文件会滞留——本模块
常驻定时器按 refine.batch_scan.interval_min（分钟）周期扫描 status='new'
的文件，逐文件走 pipeline.refine_one（管线编排唯一出口）批量提炼，防记忆滞留。

与 Dream（engine/dream.py）的关系：Dream 每日一次低峰兜底批处理（补提炼 +
TTL 标记 + 冷归档 + 日报），batch_scan 是高频轻量兜底（默认 10 分钟一轮，
只提炼不做生命周期）。两者**共用同一把提炼锁**（dream.RUN_LOCK——设计文档
「与 refine 批量提炼共用同一把锁」的接线点，dream.py 已暴露供平滑接线）：
任一方执行中，另一方非阻塞获取失败即跳过本轮，互斥保证不并发跑提炼。

调度：与 Dream/backup_scheduler 同模式——幂等常驻 daemon 线程，
``ensure_scheduler`` 幂等启动，循环复查共享配置（cfg = app.state.cfg，
可运行时经 /v1/admin/config 修改，下个周期生效）。服务启动时若
batch_scan.enabled=true 由 server/app.py 生命周期拉起；enabled=false 不启动
（运行中改 false 则到点跳过执行）。

与既有提炼入口协同（水位/状态互斥，验收 ③）：
- refine_on_append：append 后单文件后台提炼（pipeline._maybe_refine_on_append）。
  batch_scan 只扫 status='new'；联动提炼完成后 status='refined' 退出扫描视野，
  不抢同一文件。极端并发（同一文件被两路同时提炼）由 last_refined_seq 游标
  幂等性兜底——两路提取同一增量段，重复记忆经 L1.5 冲突裁决（memory_id/
  source_ref 锚点）合并/跳过，不产生脏数据。
- 手动 refine_trigger / refine_batch（operations → pipeline.async_refine_worker）：
  与 batch_scan 同扫 status='new'，并发窗口下同一文件可能被两路同时提炼，
  幂等语义同上。batch_scan 自身持共享锁防与 Dream 并发；手动链路未持锁
  （pipeline 侧改动超出本任务范围，见 dream.py 同款注释）——最坏情况为
  同一增量段被重复 L1 提取一次，L1.5 按锚点去重/合并，可接受。
- 失败文件：终态失败（LLM 全链失败/L0 解析失败）由 refine_file 标记
  status='error'，本模块不再重扫（与手动链路/Dream 语义一致）；可重试失败
  （块级缺口 / persist 落库异常）保持 status=new 由本模块下轮重扫，但同一文件
  累计失败达 refine.max_file_attempts（默认 5）→ 转 status='error' 终止重扫
  （E3 失败熔断，2026-11-05：此前无上限重扫曾空转 5 天 / 4.9 万次）；
  恢复由上层（agent 手动重触发）负责。

失败预案：单文件异常独立 try/except 继续（崩溃只丢当前文件）；整体崩溃由
status='new' 幂等语义保证下轮重扫（绝不半途标记已处理）；error 事件进
signal_events（{type:'batch_scan_error', source:'batch_scan'}，SCSM pull
可感知）。Wave 1（PR#1）已引入 LLM 节流器（rules.throttle 默认
enabled=true/rps=0.5）+ rate_limit 退避，批量触发安全。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from sgme import config as sgme_config
from sgme.data import session_dao
from sgme.engine import dream as dream_mod
from sgme.engine import pipeline as pipeline_mod
from sgme.engine import refine as refine_mod

logger = logging.getLogger("sgme.engine.batch_scan")

#: 默认扫描间隔（分钟；refine.batch_scan.interval_min 覆盖）
DEFAULT_INTERVAL_MIN = 10

#: 单轮扫描上限（status=new 文件数；防 LLM 批量撞限流）
DEFAULT_MAX_FILES = 50

#: 错误列表上限（signal 事件载荷裁剪用）
_MAX_ERRORS_IN_EVENT = 10

#: 提炼共享锁（Dream 同款：批量提炼与 Dream 不得并发——设计文档接线点）
_REFINE_LOCK = dream_mod.RUN_LOCK

#: 定时器线程守卫（ensure_scheduler 幂等）
_scheduler_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop: threading.Event | None = None


# ======================================================================
# 单次扫描执行（pipeline.refine_one 逐文件串联，单文件独立容错）
# ======================================================================

def run_batch_scan(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    max_files: int | None = None,
) -> dict[str, Any]:
    """单次兜底扫描（阻塞执行）。提炼共享锁被占用 → 直接返回 running（防重入）。

    Args:
        mem_conn: memory.db 连接（L1.5/L2 落库）。
        session_conn: session.db 连接（raw_files 队列）。
        cfg: 运行时配置（refine.batch_scan 段 + 提炼链路）。
        max_files: 单轮上限（缺省 DEFAULT_MAX_FILES）。

    Returns:
        运行摘要 dict：status / scanned / refined / failed / memory_count / errors。
        - scanned：本轮扫描的 status='new' 文件数
        - refined：status='refined' 的文件数
        - failed：失败文件数（业务失败或异常）
        - memory_count：新增记忆数（L1.5 stored 累计；降级直存同样计入）
        - errors：错误列表 [{file_id, error}]（最多 _MAX_ERRORS_IN_EVENT 条）
    """
    if not _REFINE_LOCK.acquire(blocking=False):
        logger.info("Batch 兜底扫描跳过：提炼锁被占用（Dream/其他批量提炼执行中）")
        return {
            "status": "running",
            "message": "提炼锁被占用，跳过本轮",
            "scanned": 0, "refined": 0, "failed": 0, "memory_count": 0,
            "errors": [],
        }
    try:
        return _run_batch_scan_locked(mem_conn, session_conn, cfg, max_files)
    finally:
        _REFINE_LOCK.release()


def _record_failure(
    session_conn: sqlite3.Connection,
    file_id: str,
    error: str | None,
    cfg: dict[str, Any],
) -> None:
    """E3 失败熔断（2026-11-05）：批扫侧失败记账（写库异常不打断本轮扫描）。

    refine 入口内部的失败已由 refine_file 自行记账（RefineResult.failure_recorded）；
    本函数只覆盖「refine 入口之外的异常」与未记账结果的兜底，防同一文件重复计数
    由调用方判定。
    """
    try:
        refine_mod.record_refine_failure(session_conn, file_id, error, cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("失败熔断计数写入异常（不阻塞批扫）: file=%s err=%s", file_id, e)


def _run_batch_scan_locked(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    max_files: int | None,
) -> dict[str, Any]:
    """提炼锁已持有的扫描体（run_batch_scan 内部使用，勿直接调用）。"""
    # 首链熔断（2026-11-05）：熔断期内整轮暂停——此时任何提炼都会降级到付费
    # 备用模型（生产实测一天 975 次）；文件保持 status=new，冷却后自动重试。
    from sgme.llm import chain as llm_chain

    if llm_chain.is_primary_open("refinement"):
        logger.warning("Batch 兜底扫描暂停：提炼首链熔断中（避免降级烧付费模型）")
        return {
            "status": "deferred",
            "message": "首链熔断中，暂停本轮（文件保持 status=new）",
            "scanned": 0, "refined": 0, "failed": 0, "memory_count": 0,
            "errors": [],
        }
    limit = int(max_files or DEFAULT_MAX_FILES)
    # D-80（2026-11-06）：先补跑上一次留下的失败块（逐块，不重提整份文件），
    # 再扫 status=new——两者共享提炼锁，补跑失败不阻塞常规扫描。
    replay_stats: dict[str, Any] = {}
    try:
        from sgme.engine import chunk_replay

        replay_stats = chunk_replay.replay_pending(mem_conn, session_conn, cfg)
    except Exception as e:  # noqa: BLE001——补跑是旁路，失败不影响本轮扫描
        logger.warning("块级补跑异常（不影响本轮扫描）: %s", e)
    new_files = session_dao.list_by_status(session_conn, "new", limit=limit)
    refined_count = 0
    failed_count = 0
    memory_count = 0
    errors: list[dict] = []
    for rf in new_files:
        file_id = rf["file_id"]
        try:
            # refine_one = refine_file（L1 抽取）→ persist_memories（L1.5 落库 + L2）
            # → commit_refine（游标推进，T-223：落库成功后才推进）
            result, l3_stats = pipeline_mod.refine_one(file_id, mem_conn, session_conn, cfg)
        except Exception as e:
            logger.warning("Batch 兜底提炼文件 %s 异常（继续下一文件）: %s", file_id, e)
            failed_count += 1
            if len(errors) < _MAX_ERRORS_IN_EVENT:
                errors.append({"file_id": file_id, "error": f"异常: {e}"})
            # E3 失败熔断（2026-11-05）：入口之外的异常（如 persist 落库失败）此前
            # 只让文件保持 status=new 无限重扫——计入失败计数，达上限转 error。
            _record_failure(session_conn, file_id, f"异常: {e}", cfg)
            continue
        # 首链熔断（2026-11-05）：本文件被延后（未真正提炼）→ 不计失败，
        # 本轮剩余文件同样会被延后，直接收尾等待冷却。
        if getattr(result, "deferred", False):
            logger.warning(
                "Batch 首链熔断中，暂停本轮剩余文件（已处理 %d 个，保持 status=new）",
                refined_count + failed_count,
            )
            break
        if result.status == "refined":
            refined_count += 1
            memory_count += int((l3_stats or {}).get("stored", 0) or 0)
        else:
            failed_count += 1
            if len(errors) < _MAX_ERRORS_IN_EVENT:
                errors.append({
                    "file_id": file_id,
                    "error": result.error or f"提炼失败（status={result.status}）",
                })
            # E3：refine 入口内的失败（L0 解析 / LLM 全链 / 块级缺口）已由
            # refine_file 记账（failure_recorded=True）——此处只补记未记账的结果，
            # 防同一文件被重复计数。
            if not getattr(result, "failure_recorded", False):
                _record_failure(
                    session_conn, file_id,
                    result.error or f"提炼失败（status={result.status}）", cfg,
                )
    if new_files:
        logger.info(
            "Batch 兜底扫描完成 scanned=%d refined=%d failed=%d memories=%d",
            len(new_files), refined_count, failed_count, memory_count,
        )
    if failed_count > 0:
        _publish_batch_scan_error(mem_conn, len(new_files), refined_count,
                                  failed_count, errors)
    return {
        "status": "done",
        "scanned": len(new_files),
        "refined": refined_count,
        "failed": failed_count,
        "memory_count": memory_count,
        "errors": errors,
        # D-80：块级补跑（失败块逐块重试，不重提整份文件）
        "replayed": int(replay_stats.get("replayed", 0) or 0),
        "replay_resolved": int(replay_stats.get("resolved", 0) or 0),
        "replay_pending": int(replay_stats.get("pending", 0) or 0),
    }


def _publish_batch_scan_error(
    mem_conn: sqlite3.Connection,
    scanned: int,
    refined: int,
    failed: int,
    errors: list[dict],
) -> None:
    """error 级事件进 signal_events（{type:'batch_scan_error', ...}，SCSM pull 可感知）。"""
    try:
        from sgme.signal import engine as signal_engine
        signal_engine.publish(
            event_type="batch_scan_error",
            source="batch_scan",
            payload={
                "scanned": scanned,
                "refined": refined,
                "failed": failed,
                "errors": errors,
            },
            mem_conn=mem_conn,
        )
    except Exception as e:
        logger.warning("Batch 兜底错误信号发布失败（不阻塞）: %s", e)


def is_running() -> bool:
    """Batch 兜底扫描是否正在执行（防重入观测用）。"""
    return _REFINE_LOCK.locked()


# ======================================================================
# 定时器（daemon 内常驻线程，与 Dream/backup_scheduler 同模式）
# ======================================================================

def _scheduler_loop(
    cfg: dict[str, Any],
    stop_event: threading.Event | None = None,
    data_dir: str | Path | None = None,
) -> None:
    """定时器线程体：按 refine.batch_scan.interval_min 周期执行 run_batch_scan，循环。

    - interval_min：每轮重新读取共享 cfg（可运行时经 /v1/admin/config 修改）
    - enabled=false：到点跳过执行（开关可运行时切换）
    - stop_event：测试用（None = 生产常驻）

    v1.0 连接隔离修复（2026-08-11）：线程**自建独立连接**
    （db_mod.init_databases(data_dir)），不再共享宿主 app.state 连接——
    多调度器线程（Dream/batch_scan/backup）共享同一 sqlite 连接在 Windows 上
    存在 access violation 竞态：执行中连接被宿主关闭或并发 execute 即原生崩溃
    （Wave 2 PR#8 引入 batch_scan 线程后全量测试实锤）。自建连接随线程生命周期，
    线程退出时关闭；宿主关闭连接与本线程无关。
    """
    from sgme.data import db as db_mod

    d = Path(data_dir) if data_dir else sgme_config.DATA_DIR
    mem_conn, session_conn, _ = db_mod.init_databases(d)
    try:
        while stop_event is None or not stop_event.is_set():
            # 连接有效性探测（防御保留：自建连接不存在宿主关闭，仍防异常态）
            try:
                mem_conn.execute("SELECT 1")
            except Exception:
                return
            refine_cfg = cfg.get("refine", {}) or {}
            bs = refine_cfg.get("batch_scan", {}) or {}
            try:
                interval_min = float(bs.get("interval_min", DEFAULT_INTERVAL_MIN)
                                     or DEFAULT_INTERVAL_MIN)
            except (TypeError, ValueError):
                interval_min = DEFAULT_INTERVAL_MIN
            if interval_min <= 0:
                interval_min = DEFAULT_INTERVAL_MIN  # 防御：防 0 间隔忙轮询
            wait = interval_min * 60.0
            if stop_event is not None:
                if stop_event.wait(wait):
                    return
            else:
                time.sleep(wait)
            if not bs.get("enabled", True):
                continue
            try:
                run_batch_scan(mem_conn, session_conn, cfg)
            except Exception as e:
                logger.exception("Batch 兜底扫描定时执行异常（下轮重试）: %s", e)
    finally:
        try:
            db_mod.close(mem_conn)
            db_mod.close(session_conn)
        except Exception:
            pass


def ensure_scheduler(
    cfg: dict[str, Any],
    data_dir: str | Path | None = None,
) -> bool:
    """幂等启动 Batch 兜底扫描定时器线程（daemon）。已启动返回 False。

    服务启动接线（server/app.py 生命周期，enabled=true 时）调用；
    cfg 为共享可变字典（app.state.cfg），配置改动下个周期生效。
    data_dir：线程自建连接的数据库目录（缺省 sgme_config.DATA_DIR）。
    """
    global _scheduler_thread, _scheduler_stop
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _scheduler_stop = threading.Event()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            args=(cfg, _scheduler_stop, data_dir),
            daemon=True,
            name="sgme-batch-scan-scheduler",
        )
        _scheduler_thread.start()
        logger.info(
            "Batch 兜底扫描定时器已启动（interval_min=%s）",
            ((cfg.get("refine", {}) or {}).get("batch_scan", {}) or {}).get(
                "interval_min", DEFAULT_INTERVAL_MIN),
        )
        return True


def stop_scheduler(timeout: float = 5.0) -> bool:
    """停止 Batch 兜底扫描定时器线程（幂等；测试/关停用，生产常驻可不调）。

    置位 stop_event 并 join 等待线程退出——线程当前可能在 sleep/wait
    长周期，join 超时返回 False（不强制杀，daemon 线程随进程退出）。
    """
    global _scheduler_thread, _scheduler_stop
    with _scheduler_lock:
        if _scheduler_thread is None or not _scheduler_thread.is_alive():
            _scheduler_thread = None
            return True
        if _scheduler_stop is not None:
            _scheduler_stop.set()
        _scheduler_thread.join(timeout)
        if not _scheduler_thread.is_alive():
            _scheduler_thread = None
            return True
        return False
