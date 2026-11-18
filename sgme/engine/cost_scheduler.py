"""engine/cost_scheduler.py：LLM 用量每日汇总定时器（D-63 · T-421，2026-11-18）。

复用 backup_scheduler 的内置守护定时器模式（**D-7 口径，不引 APScheduler**）：
幂等常驻 daemon 线程，到点执行「refine_runs → llm_usage_daily 回填 + 成本告警评估」。
这是账本的**兜底写入方**——实时记账调用点（refine finish 后）缺位期间的每日闭环，
保证 ``GET /v1/admin/ops/llm-cost`` 有当日汇总可看。

配置（cost 段，默认值/合并单一实现在 engine.cost_alert.cost_cfg）：
- ``cost.summary.enabled``（默认 **false**，新开关红线）：false 时线程照常守候
  但到点只跳过执行（同 backup.enabled 语义，配置可运行时切换）
- ``cost.summary.schedule``（默认 "04:00"）：每日汇总时刻，避开 Dream 03:00；
  与备份 04:00 同点但互不依赖（账本写 memory.db，快照走 backup API，无锁竞争）

连接生命周期：线程自建独立连接（db_mod.init_databases），不共享宿主连接
（Windows 多线程共享 sqlite 连接竞态，v1.0 连接隔离修复同款）；线程退出时关闭。
记账是旁路：run_cost_summary 内部异常只记日志，不影响调度线程存活（下轮重试）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sgme import config as sgme_config

logger = logging.getLogger("sgme.engine.cost_scheduler")

DEFAULT_SCHEDULE = "04:00"  # 避开 Dream 03:00（与 backup 同口径）

#: 定时器线程守卫（ensure_scheduler 幂等）
_scheduler_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop: threading.Event | None = None


def _seconds_until(schedule: str) -> float:
    """距下次 HH:MM（本地时区）的秒数；非法格式回退 1 小时后重试。"""
    now = datetime.now()
    try:
        h, m = schedule.strip().split(":", 1)
        target = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    except (ValueError, TypeError):
        logger.warning("cost summary schedule 格式非法（1 小时后重试）: %r", schedule)
        return 3600.0
    delta = (target - now).total_seconds()
    if delta <= 0:
        delta += 86400.0
    return delta


def run_cost_summary(cfg: dict[str, Any], mem_conn) -> dict:
    """执行一次每日成本汇总：回填 + 告警评估（记账旁路，异常由调度层兜底）。"""
    from sgme.engine import cost_alert, cost_meter

    out = cost_meter.backfill_from_refine_runs(mem_conn)
    out["budget"] = cost_alert.budget_status(mem_conn, cfg)
    try:
        cost_alert.evaluate_and_alert(mem_conn, cfg)
    except Exception as e:  # noqa: BLE001——告警失败不影响回填结果
        logger.warning("成本告警评估异常（不影响回填）: %s", e)
    logger.info(
        "LLM 用量日汇总完成：回填 %d 批 / %d 天，告警档位 %s",
        out.get("rows", 0), out.get("backfilled_days", 0),
        out.get("budget", {}).get("level", "?"),
    )
    return out


def _scheduler_loop(
    cfg: dict[str, Any],
    stop_event: threading.Event | None = None,
    data_dir: str | Path | None = None,
) -> None:
    """定时器线程体：按 cost.summary.schedule 到点执行 run_cost_summary，循环。

    - schedule 为空：长眠 1 小时后复查（配置可被 API 修改）
    - cost.summary.enabled=false（默认）：到点跳过执行（开关可运行时切换）
    - stop_event：测试用（None = 生产常驻）
    - 连接隔离：线程自建独立连接，退出时关闭（同 backup_scheduler 修复）
    """
    from sgme.engine import cost_alert
    from sgme.data import db as db_mod

    d = Path(data_dir) if data_dir else sgme_config.DATA_DIR
    mem_conn, _session_conn, _wiki_conn = db_mod.init_databases(d)
    try:
        while stop_event is None or not stop_event.is_set():
            cost_c = cost_alert.cost_cfg(cfg)
            schedule = cost_c["summary"]["schedule"]
            if not schedule:
                if stop_event is not None:
                    if stop_event.wait(3600):
                        return
                else:
                    time.sleep(3600)
                continue
            wait = _seconds_until(schedule)
            if stop_event is not None:
                if stop_event.wait(wait):
                    return
            else:
                time.sleep(wait)
            if not cost_c["summary"]["enabled"]:
                continue
            try:
                run_cost_summary(cfg, mem_conn)
            except Exception as e:  # noqa: BLE001——记账旁路：单轮失败不炸线程
                logger.exception("成本日汇总定时执行异常（下次到点重试）: %s", e)
    finally:
        try:
            db_mod.close(mem_conn)
        except Exception:
            pass


def ensure_scheduler(
    cfg: dict[str, Any],
    data_dir: str | Path | None = None,
) -> bool:
    """幂等启动成本汇总定时器线程（daemon）。已启动返回 False。

    生产接线：Gateway 启动时调用（enabled=false 时线程守候不执行，
    配置热生效后下个周期自动开始汇总）。
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
            name="sgme-cost-scheduler",
        )
        _scheduler_thread.start()
        from sgme.engine import cost_alert

        summary = cost_alert.cost_cfg(cfg)["summary"]
        logger.info(
            "成本汇总定时器已启动（schedule=%s, enabled=%s）",
            summary["schedule"], summary["enabled"],
        )
        return True


def stop_scheduler(timeout: float = 5.0) -> bool:
    """停止成本汇总定时器线程（幂等；测试/关停用）。"""
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
