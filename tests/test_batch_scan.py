"""tests/test_batch_scan.py：ST-23② Batch 兜底自动提炼（扫描 + 定时器 + 启动接线）。

覆盖验收标准：
1. 定时器触发逻辑（假时钟/直接调扫描函数）——test_run_batch_scan_* / test_scheduler_*
2. 扫描→提炼调用（status=new → refined 状态流转证明 refine_one 被调用）
3. 单文件失败容错（异常与业务失败均不中断其余文件）
4. enabled=false 不启动（app 接线层 + 定时器到点跳过）
5. 与 Dream 共享提炼锁互斥（锁被占用 → 跳过本轮）

测试基建（复用 test_dream 的零 LLM trick）：raw 文件消息数与 last_refined_seq
相等 → refine_file 提取到空增量直接标记 refined，不触发 L1 LLM 链路。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao
from sgme.engine import batch_scan as bs_mod
from sgme.engine import dream as dream_mod
from sgme.raw import store as raw_store
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"


# ---------- fixtures ----------

@pytest.fixture(autouse=True)
def _stop_scheduler_after():
    """每个测试后停止 Batch 兜底扫描定时器线程（防跨测试/跨文件连接泄漏崩溃）。"""
    yield
    bs_mod.stop_scheduler(timeout=2.0)


@pytest.fixture
def cfg(tmp_path):
    """load_config + raw 目录隔离到 tmp（防真实 data/ 触碰）。"""
    base = sgme_config.load_config()
    base["refine"]["batch_scan"]["enabled"] = True
    base["refine"]["batch_scan"]["interval_min"] = 10
    return base


@pytest.fixture
def conns(tmp_path, cfg, monkeypatch):
    """隔离的 memory.db / session.db / wiki.db + raw/ 根目录。"""
    monkeypatch.setattr(sgme_config, "RAW_DIR", tmp_path / "raw")
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    # 容忍测试内已手动 close 的连接（如 conn-closed 测试）
    for c in (mem_conn, session_conn, wiki_conn):
        try:
            db_mod.close(c)
        except Exception:
            try:
                c.close()
            except Exception:
                pass


# ---------- 工具函数 ----------

def _seed_raw_file(
    session_conn: sqlite3.Connection,
    *,
    file_id: str,
    session_key: str = "batch-scan-test",
    status: str = "new",
    write_disk: bool = True,
    n_msgs: int = 2,
) -> None:
    """构造 raw_files 行（+ 可选磁盘 L0 文件）。

    磁盘文件消息数与 last_refined_seq 相等 → refine_file 提取到空增量，
    不触发 L1 LLM 链路（零 LLM 测试基建）。
    """
    msgs = [
        {"timestamp": f"2026-08-01T00:00:0{i}Z", "role": "user", "content": f"消息 {i}"}
        for i in range(1, n_msgs + 1)
    ]
    if write_disk:
        raw_store.write_new_file(
            file_id=file_id,
            session_key=session_key,
            started_at="2026-08-01T00:00:00Z",
            agent_id=None,
            source_type="session",
            first_messages=msgs,
        )
    path = raw_store.relative_path(file_id) if write_disk else f"raw/sessions/{file_id}.md"
    session_dao.insert_raw_file(
        session_conn,
        file_id=file_id,
        path=path,
        session_key=session_key,
        started_at="2026-08-01T00:00:00Z",
        status=status,
        last_refined_seq=n_msgs,
        size=0,
        content_hash=None,
    )


def _raw_status(session_conn: sqlite3.Connection, file_id: str) -> str:
    row = session_dao.get_raw_file(session_conn, file_id)
    return row["status"] if row else "MISSING"


def _wait_until(fn, timeout: float = 8.0, interval: float = 0.05) -> bool:
    """轮询等待后台线程产出的条件成立（替代固定 sleep）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


# ---------- 1. 单次扫描执行 ----------

def test_run_batch_scan_refines_new_files(conns, cfg):
    """验收 2：扫 status=new → 逐文件提炼（状态流转 new→refined 证明 refine_one 被调用）。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-1")
    _seed_raw_file(session_conn, file_id="f-2")
    # refined 文件不在扫描范围（幂等：不重复提炼）
    _seed_raw_file(session_conn, file_id="f-done", status="refined")

    summary = bs_mod.run_batch_scan(mem_conn, session_conn, cfg)

    assert summary["status"] == "done"
    assert summary["scanned"] == 2
    assert summary["refined"] == 2
    assert summary["failed"] == 0
    assert _raw_status(session_conn, "f-1") == "refined"
    assert _raw_status(session_conn, "f-2") == "refined"
    assert _raw_status(session_conn, "f-done") == "refined"  # 未被重扫


def test_run_batch_scan_empty_queue(conns, cfg):
    """无 status=new 文件 → 空跑不报错。"""
    mem_conn, session_conn, _ = conns
    summary = bs_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert summary["status"] == "done"
    assert summary["scanned"] == 0
    assert summary["refined"] == 0


def test_run_batch_scan_single_file_failure_continues(conns, cfg):
    """验收 3a：单文件业务失败（磁盘缺失 → L0 解析失败）→ 其余继续 + 错误进 signal_events。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-broken", write_disk=False)  # 磁盘缺失
    _seed_raw_file(session_conn, file_id="f-good")

    summary = bs_mod.run_batch_scan(mem_conn, session_conn, cfg)

    assert summary["status"] == "done"
    assert summary["refined"] == 1
    assert summary["failed"] == 1
    assert _raw_status(session_conn, "f-broken") == "error"
    assert _raw_status(session_conn, "f-good") == "refined"
    assert any(e["file_id"] == "f-broken" for e in summary["errors"])

    # error 级事件进 signal_events（type='batch_scan_error'）
    ev = mem_conn.execute(
        "SELECT * FROM signal_events WHERE type='batch_scan_error' AND source='batch_scan'"
    ).fetchone()
    assert ev is not None
    payload = json.loads(ev["payload"])
    assert payload["failed"] == 1
    assert payload["refined"] == 1


def test_run_batch_scan_exception_tolerant(conns, cfg, monkeypatch):
    """验收 3b：单文件提炼抛异常 → 不中断其余文件（monkeypatch 注入异常）。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-boom")
    _seed_raw_file(session_conn, file_id="f-ok")

    real_refine_one = bs_mod.pipeline_mod.refine_one
    calls: list[str] = []

    def _flaky(file_id, mem, sess, c):
        calls.append(file_id)
        if file_id == "f-boom":
            raise RuntimeError("LLM boom")
        return real_refine_one(file_id, mem, sess, c)

    monkeypatch.setattr(bs_mod.pipeline_mod, "refine_one", _flaky)

    summary = bs_mod.run_batch_scan(mem_conn, session_conn, cfg)

    assert calls == ["f-boom", "f-ok"]           # 逐文件顺序调用
    assert summary["refined"] == 1
    assert summary["failed"] == 1
    assert any("LLM boom" in e["error"] for e in summary["errors"])
    assert _raw_status(session_conn, "f-ok") == "refined"


def test_run_batch_scan_respects_max_files(conns, cfg):
    """单轮上限：max_files 截断扫描量。"""
    mem_conn, session_conn, _ = conns
    for i in range(3):
        _seed_raw_file(session_conn, file_id=f"f-{i}")

    summary = bs_mod.run_batch_scan(mem_conn, session_conn, cfg, max_files=2)

    assert summary["scanned"] == 2
    assert summary["refined"] == 2
    assert _raw_status(session_conn, "f-2") == "new"  # 剩余文件留待下轮


def test_run_batch_scan_lock_held_skips(conns, cfg):
    """验收 3c（互斥）：提炼共享锁被占用（Dream 执行中）→ 跳过本轮，不处理文件。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-1")

    with dream_mod.RUN_LOCK:  # 模拟 Dream 正在执行（同一把锁）
        summary = bs_mod.run_batch_scan(mem_conn, session_conn, cfg)

    assert summary["status"] == "running"
    assert summary["scanned"] == 0
    assert _raw_status(session_conn, "f-1") == "new"  # 未被提炼
    # 锁释放后正常执行
    summary2 = bs_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert summary2["status"] == "done"
    assert _raw_status(session_conn, "f-1") == "refined"


# ---------- 2. 定时器 ----------

def test_scheduler_loop_triggers(conns, cfg, monkeypatch, tmp_path):
    """验收 1：定时器按 interval_min 到点自动扫描提炼（间隔调小加速）。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-timer")
    cfg["refine"]["batch_scan"]["interval_min"] = 0.001  # 0.06s 一轮

    stop = threading.Event()
    t = threading.Thread(
        target=bs_mod._scheduler_loop,
        args=(cfg, stop, tmp_path / "data"),
        daemon=True,
        name="test-batch-scan-scheduler",
    )
    t.start()
    assert _wait_until(lambda: _raw_status(session_conn, "f-timer") == "refined")
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_scheduler_disabled_skips(conns, cfg, tmp_path):
    """enabled=false 到点跳过执行（开关可运行时切换）。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-off")
    cfg["refine"]["batch_scan"]["enabled"] = False
    cfg["refine"]["batch_scan"]["interval_min"] = 0.001

    stop = threading.Event()
    t = threading.Thread(
        target=bs_mod._scheduler_loop,
        args=(cfg, stop, tmp_path / "data"),
        daemon=True,
        name="test-batch-scan-disabled",
    )
    t.start()
    time.sleep(0.5)  # 等待多轮到点
    stop.set()
    t.join(timeout=5)
    assert _raw_status(session_conn, "f-off") == "new"  # 从未执行


def test_ensure_scheduler_idempotent(conns, cfg, tmp_path):
    """验收 2（幂等）：ensure_scheduler 二次调用不重复启动。"""
    first = bs_mod.ensure_scheduler(cfg, data_dir=tmp_path / "data")
    second = bs_mod.ensure_scheduler(cfg, data_dir=tmp_path / "data")
    assert first is True
    assert second is False
    assert bs_mod.stop_scheduler(timeout=2.0) is True


def test_scheduler_loop_stop_event_exits(conns, cfg, tmp_path):
    """stop_event 置位 → 线程退出并关闭自建连接（连接隔离修复，2026-08-11）。"""
    stop = threading.Event()
    t = threading.Thread(
        target=bs_mod._scheduler_loop,
        args=(cfg, stop, tmp_path / "data"),
        daemon=True,
        name="test-batch-scan-stopevent",
    )
    t.start()
    assert _wait_until(lambda: t.is_alive())
    stop.set()
    assert _wait_until(lambda: not t.is_alive())


# ---------- 3. 服务启动接线（lifespan） ----------

def _make_app(cfg, conns, tmp_path, *, enabled: bool):
    mem_conn, session_conn, wiki_conn = conns
    cfg["refine"]["batch_scan"]["enabled"] = enabled
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        data_dir=tmp_path / "data",  # 调度器线程自建独立连接指向同一测试库（2026-08-11 修复）
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=True,
    )


def test_app_startup_starts_scheduler_when_enabled(conns, cfg, tmp_path):
    """验收 2/4：服务启动（生产模式）且 enabled=true → 定时器线程拉起；关停后停止。"""
    app = _make_app(cfg, conns, tmp_path, enabled=True)
    with TestClient(app):
        assert bs_mod._scheduler_thread is not None
        assert bs_mod._scheduler_thread.is_alive()
    # lifespan 关停 → 线程停止（stop_scheduler join）
    assert bs_mod._scheduler_thread is None or not bs_mod._scheduler_thread.is_alive()


def test_app_startup_skips_scheduler_when_disabled(conns, cfg, tmp_path):
    """验收 4：enabled=false → 服务启动不拉起定时器。"""
    app = _make_app(cfg, conns, tmp_path, enabled=False)
    with TestClient(app):
        assert bs_mod._scheduler_thread is None
