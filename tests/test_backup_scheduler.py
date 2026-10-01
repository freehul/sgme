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


# ---------- T-228/M-1：相对路径口径（USER_ROOT，与进程 CWD 无关） ----------

def _isolate_user_root(tmp_path, monkeypatch):
    """把 USER_ROOT/RAW_DIR 指到 tmp（相对路径解析隔离夹具，T-228）。"""
    import sgme.config as sgme_config

    user_root = tmp_path / "user_root"
    user_root.mkdir()
    raw = tmp_path / "raw_isolated"
    raw.mkdir()
    monkeypatch.setattr(sgme_config, "USER_ROOT", user_root)
    monkeypatch.setattr(sgme_config, "RAW_DIR", raw)
    return user_root


def test_run_backup_relative_dir_resolves_against_user_root(backup_env, tmp_path, monkeypatch):
    """相对 dir 基于 USER_ROOT 解析、与进程 CWD 无关（T-228/M-1）。

    旧实现把相对 dir 直接交给 create_snapshot 按进程工作目录解析——Docker
    WORKDIR=/app 时快照落容器层 /app/data/backups 而非 /data 卷，容器重建即丢。
    """
    cfg, mem_conn, session_conn, wiki_conn = backup_env
    user_root = _isolate_user_root(tmp_path, monkeypatch)
    cwd_elsewhere = tmp_path / "cwd_elsewhere"
    cwd_elsewhere.mkdir()
    monkeypatch.chdir(cwd_elsewhere)  # CWD 指到别处：旧实现会落这里

    cfg["backup"]["dir"] = "backups_rel"  # 相对路径
    result = bsch._run_backup(cfg, mem_conn, session_conn, wiki_conn)

    snap_dir = Path(result["path"])
    assert snap_dir == user_root / "backups_rel" / result["snapshot_id"]
    assert (snap_dir / "memory.db").exists()
    assert not (cwd_elsewhere / "backups_rel").exists(), "相对路径不应按进程 CWD 解析"


def test_run_backup_absolute_dir_unaffected_by_user_root(backup_env, tmp_path, monkeypatch):
    """绝对 dir 原样使用（不被接到 USER_ROOT 下）——T-228 回归护栏。"""
    cfg, mem_conn, session_conn, wiki_conn = backup_env
    user_root = _isolate_user_root(tmp_path, monkeypatch)
    abs_dir = tmp_path / "abs_backups"
    cfg["backup"]["dir"] = str(abs_dir)

    result = bsch._run_backup(cfg, mem_conn, session_conn, wiki_conn)

    snap_dir = Path(result["path"])
    assert snap_dir.parent == abs_dir
    assert (snap_dir / "memory.db").exists()
    assert not (user_root / "abs_backups").exists()


def test_scheduled_and_manual_same_relative_dir(backup_env, tmp_path, monkeypatch):
    """同一相对 dir：定时快照落点 == 手动 _resolve_backup_dir（同口径，T-228）。"""
    from sgme.operations.backup import _resolve_backup_dir

    cfg, mem_conn, session_conn, wiki_conn = backup_env
    user_root = _isolate_user_root(tmp_path, monkeypatch)
    cfg["backup"]["dir"] = "shared/backups"

    result = bsch._run_backup(cfg, mem_conn, session_conn, wiki_conn)
    manual_dir = _resolve_backup_dir(cfg)

    assert Path(result["path"]).parent == manual_dir
    assert manual_dir == user_root / "shared" / "backups"


# ---------- 服务启动接线（lifespan，T-209） ----------
# 修复前：backup_scheduler 唯一拉起点在 POST /v1/admin/backup（routes_backup.py），
# 容器重启后若无人调备份接口，调度线程永不创建 → 每日 04:00 备份静默失效
# （与 B117 修 Dream 前同款隐患，观察项 G-7 同源）。修复：接入 app.py lifespan
# 生产模式——启动即拉起、关停即停止，全程无需触碰任何备份 API。

_ADMIN_KEY = "test-admin-key"
_AGENT_KEY = "test-agent-key"


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """T-209 接线测试：完整配置（load_config 基底）+ 全落点隔离到 tmp。"""
    from sgme import config as sgme_config
    from sgme.data import db as db_mod
    from sgme.data import memory_dao

    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", raw)

    cfg = sgme_config.load_config()
    cfg["paths"]["data_dir"] = str(tmp_path / "data")
    cfg["backup"]["dir"] = str(tmp_path / "backups")

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield cfg, mem_conn, session_conn, wiki_conn
    for c in (mem_conn, session_conn, wiki_conn):
        try:
            c.close()
        except Exception:
            pass


def _make_app(cfg, conns, tmp_path, *, enabled: bool):
    """构造生产模式（start_background_tasks=True）的 app（T-209）。"""
    from sgme.server.app import create_app

    mem_conn, session_conn, wiki_conn = conns
    cfg["backup"]["enabled"] = enabled
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        data_dir=tmp_path / "data",
        admin_key=_ADMIN_KEY,
        agent_key=_AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=True,
    )


def test_lifespan_starts_backup_scheduler_without_api_call(app_env, tmp_path):
    """T-209 核心验收：生产模式启动即拉起备份定时器（无需触碰任何备份 API）；关停后停止。"""
    from fastapi.testclient import TestClient

    assert bsch._scheduler_thread is None, "前置：不应有残留备份调度线程"
    cfg, mem_conn, session_conn, wiki_conn = app_env
    app = _make_app(cfg, (mem_conn, session_conn, wiki_conn), tmp_path, enabled=True)
    with TestClient(app):
        assert bsch._scheduler_thread is not None
        assert bsch._scheduler_thread.is_alive()
    # lifespan 关停 → stop_scheduler 生效（join 后置 None 或线程已退出）
    assert bsch._scheduler_thread is None or not bsch._scheduler_thread.is_alive()


def test_lifespan_skips_backup_scheduler_when_disabled(app_env, tmp_path):
    """backup.enabled=false → 服务启动不拉起备份定时器（门控对齐 batch_scan）。"""
    from fastapi.testclient import TestClient

    cfg, mem_conn, session_conn, wiki_conn = app_env
    app = _make_app(cfg, (mem_conn, session_conn, wiki_conn), tmp_path, enabled=False)
    with TestClient(app):
        assert bsch._scheduler_thread is None
