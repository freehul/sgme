"""engine/backup_scheduler.py：每日自动备份定时器（0.8 方案 B，2026-08-10）。

复用 Dream 定时器模式（engine/dream.py 同构）：幂等常驻 daemon 线程，
到点执行 本地快照（create_snapshot）→ 轮转（rotate_snapshots）→
异地推送（push_remote，remote_dir 可空 = 跳过）。

触发链：/v1/admin/backup/create 端点接线 ensure_scheduler 幂等拉起；
生产 Gateway 首次备份后定时器即常驻（同 Dream 的 ensure_scheduler 约定）。

连接生命周期：线程自建独立连接（db_mod.init_databases(data_dir)），
不共享宿主 app.state 连接（v1.0 连接隔离修复，2026-08-14——对齐
batch_scan.py/dream.py：Windows 多线程共享 sqlite 连接存在 access
violation 竞态）。线程退出时关闭自建连接；宿主关闭连接与本线程无关。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sgme.backup import manager as backup_manager

logger = logging.getLogger(__name__)

# 默认备份目录（config/sgme.yaml backup.dir 覆盖；remote_dir 可空）
DEFAULT_BACKUP_DIR = "data/backups"
DEFAULT_SCHEDULE = "04:00"  # 避开 Dream 03:00
DEFAULT_KEEP_FULL = 7

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
        logger.warning("backup schedule 格式非法（1 小时后重试）: %r", schedule)
        return 3600.0
    delta = (target - now).total_seconds()
    if delta <= 0:
        delta += 86400.0
    return delta


def _run_backup(cfg: dict[str, Any], mem_conn, session_conn, wiki_conn) -> dict:
    """执行一次完整备份：快照 → 轮转 → 异地推送（失败不阻塞后续）。

    Returns:
        {snapshot_id, level, rotated, remote}
    """
    backup_cfg = cfg.get("backup", {}) or {}
    level = backup_cfg.get("level", "incremental")
    dir_str = backup_cfg.get("dir", DEFAULT_BACKUP_DIR)
    keep_full = int(backup_cfg.get("keep_full", DEFAULT_KEEP_FULL))
    remote_dir = backup_cfg.get("remote_dir") or None

    # 1) 一致快照（SQLite backup API 免停机；传三库连接避免临时裸连接）
    snap = backup_manager.create_snapshot(
        cfg.get("paths", {}).get("data_dir", "data"),
        dir_str,
        level=level,
        conn_pair=(mem_conn, session_conn, wiki_conn),
        id_prefix="daily_",
    )
    # 2) 轮转（full 保留 keep_full 份；incremental/pre_restore 不轮转）
    rotated = backup_manager.rotate_snapshots(dir_str, keep_full=keep_full)
    # 3) 异地推送（remote_dir 为空 = 跳过；失败仅记录不阻塞）
    remote = backup_manager.push_remote(snap["path"], remote_dir)
    return {
        "snapshot_id": snap["snapshot_id"],
        "level": snap["level"],
        "path": str(snap["path"]),
        "rotated": rotated,
        "remote": remote,
    }


def _scheduler_loop(
    cfg: dict[str, Any],
    stop_event: threading.Event | None = None,
    data_dir: str | Path | None = None,
) -> None:
    """定时器线程体：按 backup.schedule 到点执行 _run_backup，循环。

    - schedule 为空（不自动只手动）：长眠 1 小时后复查（配置可被 API 修改）
    - enabled=false：到点跳过执行（开关可运行时切换）
    - stop_event：测试用（None = 生产常驻）

    v1.0 连接隔离修复（2026-08-14）：线程自建独立连接
    （db_mod.init_databases(data_dir)），不再共享宿主 app.state 连接——
    与 batch_scan/dream 同款（Windows 多线程共享 sqlite 连接存在
    access violation 竞态）。线程退出时关闭自建连接。
    """
    from sgme import config as sgme_config
    from sgme.data import db as db_mod

    d = Path(data_dir) if data_dir else sgme_config.DATA_DIR
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(d)
    try:
        while stop_event is None or not stop_event.is_set():
            backup_cfg = cfg.get("backup", {}) or {}
            schedule = backup_cfg.get("schedule", DEFAULT_SCHEDULE)
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
            if not backup_cfg.get("enabled", True):
                continue
            try:
                result = _run_backup(cfg, mem_conn, session_conn, wiki_conn)
                logger.info(
                    "自动备份完成: %s（level=%s, rotated_removed=%s, remote=%s）",
                    result["snapshot_id"],
                    result["level"],
                    len(result["rotated"].get("removed", [])),
                    "ok" if result["remote"].get("ok") else "skipped/failed",
                )
            except Exception as e:
                logger.exception("自动备份定时执行异常（下次到点重试）: %s", e)
    finally:
        try:
            db_mod.close(mem_conn)
            db_mod.close(session_conn)
            db_mod.close(wiki_conn)
        except Exception:
            pass


def ensure_scheduler(
    cfg: dict[str, Any],
    data_dir: str | Path | None = None,
) -> bool:
    """幂等启动备份定时器线程（daemon）。已启动返回 False。

    手动备份端点接线时调用；生产 Gateway 首次备份后定时器即常驻。
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
            name="sgme-backup-scheduler",
        )
        _scheduler_thread.start()
        logger.info(
            "备份定时器已启动（schedule=%s）",
            (cfg.get("backup", {}) or {}).get("schedule", DEFAULT_SCHEDULE),
        )
        return True


def stop_scheduler(timeout: float = 5.0) -> bool:
    """停止备份定时器线程（幂等；测试/关停用，生产常驻可不调）。

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
