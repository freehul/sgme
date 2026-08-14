"""test_backup_scheduler.py：每日自动备份定时器测试（0.8 方案 B）。

复用 Dream 定时器可测设计（test_dream.py 同构）：
- 时间加速：monkeypatch _seconds_until → 极小值，到点立即触发
- 断言用 _wait_until 轮询（非固定 sleep）
- autouse fixture 每个测试后 stop_scheduler（防跨文件线程泄漏崩溃）
- 隔离：data 三库 init_databases(tmp_path) + backup 配置注入 tmp
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sgme.engine import backup_scheduler as bsch


# ---------- fixtures ----------

@pytest.fixture
def backup_env(tmp_path, monkeypatch):
    """隔离环境：tmp 三库 + backup 配置指向 tmp + 时间加速。"""
    import sgme.config as sgme_config
    from sgme.data import db as db_mod

    data_dir = tmp_path / "data"
    # init_databases 内部 check_same_thread=False（定时器线程需跨线程访问）
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(data_dir)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    cfg = {
        "paths": {"data_dir": str(data_dir), "raw_dir": str(raw_dir)},
        "backup": {
            "enabled": True,
            "schedule": "04:00",
            "level": "incremental",
            "dir": str(tmp_path / "backups"),
            "keep_full": 3,
            "remote_dir": "",
            "raw_cold_days": 90,
        },
    }
    yield cfg, mem_conn, session_conn, wiki_conn
    for c in (mem_conn, session_conn, wiki_conn):
        try:
            c.close()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _stop_scheduler_after():
    """每个测试后停止备份定时器线程（防跨测试/跨文件连接泄漏崩溃）。"""
    yield
    bsch.stop_scheduler()


def _wait_until(fn, timeout=8.0, interval=0.05):
    """轮询等待条件成立（替代固定 sleep）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


# ---------- 单元测试 ----------

def test_seconds_until_normal():
    """正常 HH:MM 解析返回正秒数。"""
    assert bsch._seconds_until("04:00") > 0


def test_seconds_until_invalid_fallback():
    """非法格式回退 1 小时。"""
    assert bsch._seconds_until("not-a-time") == 3600.0


def test_run_backup_creates_snapshot(backup_env):
    """_run_backup 生成快照 + 轮转 + remote 跳过。"""
    cfg, mem_conn, session_conn, wiki_conn = backup_env
    result = bsch._run_backup(cfg, mem_conn, session_conn, wiki_conn)
    assert result["snapshot_id"].startswith("daily_incremental_")
    assert result["level"] == "incremental"
    assert result["remote"]["ok"] is True
    assert result["remote"].get("skipped") is True  # remote_dir 空 = 跳过
    snap_dir = Path(result["path"])
    assert snap_dir.exists()
    assert (snap_dir / "memory.db").exists()


def test_run_backup_remote_copy(backup_env, tmp_path):
    """remote_dir 配置后快照复制到异地目录。"""
    cfg, mem_conn, session_conn, wiki_conn = backup_env
    remote = tmp_path / "remote"
    cfg["backup"]["remote_dir"] = str(remote)
    result = bsch._run_backup(cfg, mem_conn, session_conn, wiki_conn)
    assert result["remote"]["ok"] is True
    assert not result["remote"].get("skipped")
    assert (remote / result["snapshot_id"]).exists()


def test_scheduler_loop_triggers(backup_env, monkeypatch):
    """定时器到点自动执行备份（时间加速）。"""
    cfg, mem_conn, session_conn, wiki_conn = backup_env
    monkeypatch.setattr(bsch, "_seconds_until", lambda s: 0.05)
    stop = threading.Event()
    t = threading.Thread(
        target=bsch._scheduler_loop,
        args=(cfg, stop, Path(cfg["paths"]["data_dir"])),
        daemon=True,
        name="test-backup-scheduler",
    )
    t.start()
    backups = Path(cfg["backup"]["dir"])
    assert _wait_until(lambda: any(backups.glob("daily_incremental_*")))
    stop.set()
    t.join(timeout=5)


def test_scheduler_disabled_skips(backup_env, monkeypatch):
    """enabled=false 到点跳过执行（开关可运行时切换）。"""
    cfg, mem_conn, session_conn, wiki_conn = backup_env
    cfg["backup"]["enabled"] = False
    monkeypatch.setattr(bsch, "_seconds_until", lambda s: 0.05)
    stop = threading.Event()
    t = threading.Thread(
        target=bsch._scheduler_loop,
        args=(cfg, stop, Path(cfg["paths"]["data_dir"])),
        daemon=True,
        name="test-backup-disabled",
    )
    t.start()
    backups = Path(cfg["backup"]["dir"])
    time.sleep(0.5)  # 等待多轮到点
    stop.set()
    t.join(timeout=5)
    assert not any(backups.glob("daily_incremental_*"))


def test_ensure_scheduler_idempotent(backup_env):
    """ensure_scheduler 幂等：二次调用不重复启动。"""
    cfg, mem_conn, session_conn, wiki_conn = backup_env
    data_dir = Path(cfg["paths"]["data_dir"])
    first = bsch.ensure_scheduler(cfg, data_dir=data_dir)
    second = bsch.ensure_scheduler(cfg, data_dir=data_dir)
    assert first is True
    assert second is False
    assert bsch.stop_scheduler(timeout=2.0) is True


def test_scheduler_loop_stop_event_exits(backup_env):
    """stop_event 置位 → 线程退出并关闭自建连接（连接隔离修复，2026-08-14）。"""
    cfg, mem_conn, session_conn, wiki_conn = backup_env
    stop = threading.Event()
    t = threading.Thread(
        target=bsch._scheduler_loop,
        args=(cfg, stop, Path(cfg["paths"]["data_dir"])),
        daemon=True,
        name="test-backup-stopevent",
    )
    t.start()
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
