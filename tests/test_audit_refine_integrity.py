"""tests/test_audit_refine_integrity.py：0930 深度审查 P0-1/P0-2 回归（T-222 / T-223）。

覆盖三组（TDD 审计回归，全部离线：mock L1 桩 + tmp 三库，不真调 LLM）：

T-222（P0-1 共享连接显式事务并发冲突 → 记忆静默丢失）
1. 共享连接并发不丢：两线程同连接并发 insert_memory → 零报错、零丢行
   （确定性交错 + 自然并发两式；memory_dao._TXN_LOCK 收口）。
2. 线程隔离：提炼异步链路（refine_trigger_async / refine_batch 异步分支 /
   async_refine_worker / _async_batch_worker / 联动提炼）一律线程内自建独立
   连接——宿主连接不再交给后台线程，且线程退出后自建连接已关闭。

T-223（P0-2 提炼游标先于落库推进 → 崩溃后记忆永久丢失）
3. 游标后移：refine_file 只产结果不推游标；commit_refine 落库成功后才推进
   游标 + 内容哈希；error 结果不推进；无增量经 commit 收敛 refined。
4. 重试恢复：persist 故障注入 → 文件保持 status=new（RED 基线为 refined）
   → 重试重新提取并落库；batch_scan 下一轮重扫拾起同一文件。

运行（项目 venv 解释器，工作树根目录下）：
    python -m pytest tests/test_audit_refine_integrity.py -p no:cacheprovider \
        --junitxml=tmp/junit.xml
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao
from sgme.engine import batch_scan as batch_scan_mod
from sgme.engine import pipeline as pipeline_mod
from sgme.engine import refine as refine_mod
from sgme.engine.refine import RefineResult
from sgme.operations import refine as refine_op
from sgme.raw import store as raw_store


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def conns(tmp_path, cfg):
    """宿主三库连接（隔离 tmp_path/data，含维度注册表）。"""
    mem_conn, session_conn, _ = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)


# ---------- 工具 ----------

def _insert(conn, content, sources=None):
    return memory_dao.insert_memory(
        conn, content=content, memory_type="persona", priority=50,
        time_velocity="static", ttl_days=None, dimension_ids=[], sources=sources,
    )


def _rows(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT content FROM memories ORDER BY content").fetchall()]


def _assert_closed(conn):
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def _main_db_file(conn) -> str:
    for r in conn.execute("PRAGMA database_list").fetchall():
        if r[1] == "main" and r[2]:
            return str(Path(r[2]).resolve())
    raise AssertionError("连接无 main 库文件路径")


def _make_raw_file(session_conn, file_id="f-audit", messages=None,
                   last_refined_seq=None):
    """构造 raw 文件 + raw_files 行（照 test_engine._setup_raw_file）。"""
    msgs = messages or [
        {"timestamp": "2026-08-04T10:00:00Z", "role": "user", "content": "我用 Python 3.11 写 SGME 项目"},
        {"timestamp": "2026-08-04T10:01:00Z", "role": "assistant", "content": "了解，技术栈记下了"},
    ]
    raw_store.write_new_file(
        file_id=file_id, session_key="sess_audit",
        started_at="2026-08-04T10:00:00Z", agent_id="test",
        first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=raw_store.relative_path(file_id),
        session_key="sess_audit", started_at="2026-08-04T10:00:00Z",
        agent_id="test", status="new", size=raw_store.file_size(file_id),
        last_refined_seq=last_refined_seq,
    )
    return file_id


def _stub_l1_one_memory(monkeypatch, calls: list):
    """把 l1.extract_l1 替换为固定产出一条记忆（无网络、确定性）。"""
    def fake(conversations, dimensions, llm_cfg, client=None, **kwargs):
        calls.append(1)
        return ([{
            "content": "用户使用 Python 3.11",
            "dimensions": ["技术栈"],
            "memory_type": "persona", "priority": 80, "time_velocity": "static",
            "source_message_ids": [2],
        }], "mock", {"stage": "l1_extraction", "version": "test-mock", "variant": None})

    monkeypatch.setattr("sgme.engine.l1.extract_l1", fake)


# ======================================================================
# T-222 组①：共享连接并发不丢（_TXN_LOCK 收口）
# ======================================================================

def test_t222_shared_conn_interleaved_txn_no_loss(tmp_path, monkeypatch):
    """确定性交错：A 事务未提交时 B 同连接 BEGIN——两行都必须落库、零报错。

    RED（修复前）：B 报 OperationalError 且 rollback 连带回滚 A 的半程写入
    （0~1 行落库）；GREEN：互斥锁全程覆盖 BEGIN…COMMIT/ROLLBACK。
    """
    mem_conn, _, _ = db_mod.init_databases(tmp_path / "data")
    gate = threading.Event()
    orig_segment = memory_dao.segment

    def hooked_segment(text):
        # insert_memory 在 BEGIN 与 COMMIT 之间调 segment()：卡 A 制造并发窗口
        if str(text).startswith("A-") and threading.current_thread().name == "A-owner":
            gate.set()
            time.sleep(0.3)
        return orig_segment(text)

    monkeypatch.setattr(memory_dao, "segment", hooked_segment)

    errors: list = []
    returned: list = []

    def worker_a():
        try:
            returned.append(_insert(mem_conn, "A-1"))
        except Exception as e:  # noqa: BLE001
            errors.append(("A", repr(e)))

    def worker_b():
        assert gate.wait(timeout=3.0), "A 未进入事务窗口"
        try:
            returned.append(_insert(mem_conn, "B-1"))
        except Exception as e:  # noqa: BLE001
            errors.append(("B", repr(e)))

    ta = threading.Thread(target=worker_a, name="A-owner")
    tb = threading.Thread(target=worker_b, name="B-owner")
    ta.start(); tb.start(); ta.join(); tb.join()

    assert errors == [], f"并发显式事务仍互踩: {errors}"
    assert len(returned) == 2
    assert _rows(mem_conn) == ["A-1", "B-1"]


def test_t222_shared_conn_concurrent_burst_no_loss(tmp_path):
    """自然并发：两线程同连接各 8 条连续 insert → 16 行全落、零报错。"""
    mem_conn, _, _ = db_mod.init_databases(tmp_path / "data")
    errors: list = []

    def worker(tag: str):
        for i in range(8):
            try:
                _insert(mem_conn, f"{tag}-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append((tag, repr(e)))
                time.sleep(0.005)

    t1 = threading.Thread(target=worker, args=("x",))
    t2 = threading.Thread(target=worker, args=("y",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert errors == [], f"自然并发仍互踩: {errors[:3]}"
    assert mem_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 16


def test_t222_resolve_data_dir_from_conn(tmp_path):
    """resolve_data_dir：文件库 → 父目录；:memory: → None。"""
    mem_conn, _, _ = db_mod.init_databases(tmp_path / "data")
    assert db_mod.resolve_data_dir(mem_conn) == (tmp_path / "data").resolve()
    assert db_mod.resolve_data_dir(sqlite3.connect(":memory:")) is None


# ======================================================================
# T-222 组②：提炼异步链路线程内自建连接（不再共享宿主连接）
# ======================================================================

class _CaptureThread:
    """记录 target/args、不真正启动线程的替身（确定性断言用）。"""

    captured: dict = {}

    def __init__(self, target=None, args=(), daemon=None, **kwargs):
        _CaptureThread.captured = {"target": target, "args": args, "daemon": daemon}

    def start(self):
        _CaptureThread.captured["started"] = True


def test_t222_async_trigger_thread_gets_data_dir_not_host_conns(conns, cfg, monkeypatch):
    """refine_trigger_async：线程参数 = (file_id, limit, data_dir, cfg)，不含宿主连接。"""
    mem_conn, session_conn = conns
    monkeypatch.setattr(refine_op.threading, "Thread", _CaptureThread)

    res = refine_op.refine_trigger_async(mem_conn, session_conn, cfg,
                                         file_id="f-async", limit=3)

    assert res.ok is True
    cap = _CaptureThread.captured
    assert cap["started"] is True and cap["daemon"] is True
    assert cap["target"] is pipeline_mod.async_refine_worker
    f_id, limit, data_dir, c_cfg = cap["args"]
    assert (f_id, limit) == ("f-async", 3)
    assert c_cfg is cfg
    # 关键：宿主连接不进线程参数；data_dir 由宿主连接推导
    assert mem_conn not in cap["args"] and session_conn not in cap["args"]
    assert data_dir == db_mod.resolve_data_dir(session_conn) is not None


def test_t222_refine_batch_async_thread_gets_data_dir(conns, raw_dir, cfg, monkeypatch):
    """refine_batch 异步（显式列表）：线程参数 = (file_ids, data_dir, cfg)，不含宿主连接。"""
    mem_conn, session_conn = conns
    _make_raw_file(session_conn, file_id="f-batch")
    monkeypatch.setattr(refine_op.threading, "Thread", _CaptureThread)

    res = refine_op.refine_batch(mem_conn, session_conn, cfg,
                                 file_ids=["f-batch"], async_mode=True)

    assert res.ok is True
    cap = _CaptureThread.captured
    assert cap["target"] is refine_op._async_batch_worker
    f_ids, data_dir, c_cfg = cap["args"]
    assert f_ids == ["f-batch"] and c_cfg is cfg
    assert mem_conn not in cap["args"] and session_conn not in cap["args"]
    assert data_dir == db_mod.resolve_data_dir(session_conn) is not None


def test_t222_async_refine_worker_self_creates_conns(tmp_path, cfg, monkeypatch):
    """async_refine_worker：线程内自建连接（≠ 宿主连接、同库文件），退出即关闭。"""
    data_dir = tmp_path / "data"
    host_mem, host_sess, _ = db_mod.init_databases(data_dir)
    seen: dict = {}

    def fake_refine_file(file_id, mem_conn, session_conn, c, client=None, **kw):
        seen["mem"] = mem_conn
        seen["sess"] = session_conn
        seen["sess_path"] = _main_db_file(session_conn)  # 关闭前取库路径
        return RefineResult(file_id=file_id)

    monkeypatch.setattr(refine_mod, "refine_file", fake_refine_file)

    pipeline_mod.async_refine_worker("f-x", 5, data_dir, cfg)

    assert seen["mem"] is not host_mem
    assert seen["sess"] is not host_sess
    # 指向同一数据库文件（同一 data_dir 的独立连接）
    assert seen["sess_path"] == _main_db_file(host_sess)
    # 线程退出后自建连接已关闭
    _assert_closed(seen["mem"])
    _assert_closed(seen["sess"])


def test_t222_async_batch_worker_self_creates_conns(tmp_path, cfg, monkeypatch):
    """_async_batch_worker：线程内自建连接，退出即关闭。"""
    data_dir = tmp_path / "data"
    host_mem, host_sess, _ = db_mod.init_databases(data_dir)
    seen: dict = {}

    def fake_refine_one(file_id, mem_conn, session_conn, c):
        seen["mem"] = mem_conn
        seen["sess"] = session_conn
        seen["mem_path"] = _main_db_file(mem_conn)  # 关闭前取库路径
        return RefineResult(file_id=file_id), dict(pipeline_mod._ZERO_STATS)

    monkeypatch.setattr(pipeline_mod, "refine_one", fake_refine_one)

    refine_op._async_batch_worker(["f-a", "f-b"], data_dir, cfg)

    assert seen["mem"] is not host_mem and seen["sess"] is not host_sess
    assert seen["mem_path"] == _main_db_file(host_mem)
    _assert_closed(seen["mem"])
    _assert_closed(seen["sess"])


def test_t222_refine_on_append_thread_self_creates_conns(conns, cfg, monkeypatch):
    """联动提炼（refine_on_append）：后台线程自建连接，不共享宿主连接。"""
    mem_conn, session_conn = conns
    cfg_on = dict(cfg)
    cfg_on["refine"] = {**(cfg.get("refine") or {}), "refine_on_append": True}
    seen: dict = {}

    def fake_refine_one(file_id, mem_conn_w, session_conn_w, c):
        seen["mem"] = mem_conn_w
        seen["sess"] = session_conn_w
        seen["sess_path"] = _main_db_file(session_conn_w)  # 关闭前取库路径
        return RefineResult(file_id=file_id), dict(pipeline_mod._ZERO_STATS)

    monkeypatch.setattr(pipeline_mod, "refine_one", fake_refine_one)

    class _SyncThread:  # start() 即同步执行（确定性 join）
        def __init__(self, target=None, daemon=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _SyncThread)
    pipeline_mod._maybe_refine_on_append(cfg_on, mem_conn, session_conn, "f-append")

    assert seen["mem"] is not mem_conn and seen["sess"] is not session_conn
    assert seen["sess_path"] == _main_db_file(session_conn)
    _assert_closed(seen["mem"])
    _assert_closed(seen["sess"])


# ======================================================================
# T-223 组③：游标后移（commit_refine 落库成功后才推进）
# ======================================================================

def test_t223_refine_file_alone_does_not_advance_cursor(conns, raw_dir, cfg, monkeypatch):
    """refine_file 只产结果：raw_files 游标/状态/哈希均不动，commit_refine 才推进。"""
    mem_conn, session_conn = conns
    fid = _make_raw_file(session_conn, file_id="f-cursor", last_refined_seq=1)
    calls: list = []
    _stub_l1_one_memory(monkeypatch, calls)

    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)

    assert result.status == "refined"
    assert result.new_last_refined_seq == 2
    assert result.content_hash  # 结果携带哈希，供 commit 用
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "new"            # ← 未推进
    assert rf["last_refined_seq"] == 1      # ← 未推进
    assert not rf["content_hash"]           # ← 未写哈希

    # 落库成功的等价语义：显式 commit
    assert refine_mod.commit_refine(result, session_conn) is True
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "refined"
    assert rf["last_refined_seq"] == 2
    assert len(rf["content_hash"]) == 64


def test_t223_commit_refine_noop_for_error_result(conns, cfg):
    """error 结果 commit_refine 不推进（False、raw_files 原样）。"""
    mem_conn, session_conn = conns
    fid = _make_raw_file(session_conn, file_id="f-err-commit", last_refined_seq=1)
    err = RefineResult(file_id=fid, status="error", error="boom",
                       new_last_refined_seq=99)

    assert refine_mod.commit_refine(err, session_conn) is False
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "new" and rf["last_refined_seq"] == 1


def test_t223_no_incremental_converges_refined_via_commit(conns, raw_dir, cfg, monkeypatch):
    """无增量：不调 L1、不落库；经 refine_one → commit_refine 收敛为 refined。"""
    mem_conn, session_conn = conns
    fid = _make_raw_file(session_conn, file_id="f-noinc", last_refined_seq=2)
    monkeypatch.setattr(
        "sgme.engine.l1.extract_l1",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("无增量不应调 L1")),
    )

    result, _stats = pipeline_mod.refine_one(fid, mem_conn, session_conn, cfg)

    assert result.status == "refined"
    assert result.new_last_refined_seq == 2
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "refined"        # 经 commit 收敛
    assert rf["last_refined_seq"] == 2
    assert mem_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


# ======================================================================
# T-223 组④：重试恢复（persist 故障注入 → new 保持 → 重试落库）
# ======================================================================

def _flaky_persist(monkeypatch, calls: dict, fail_times: int = 1):
    """persist_memories 故障注入：前 fail_times 次抛异常，之后真实写库（直存等价）。"""

    def flaky(result, mem_conn, c, agent_tag=None):
        calls["n"] = calls.get("n", 0) + 1
        if calls["n"] <= fail_times:
            raise RuntimeError("injected persist failure（模拟磁盘满/并发错误）")
        stored = 0
        for m in result.memories:
            memory_dao.insert_memory(
                mem_conn, content=m["content"], memory_type=m.get("memory_type", "persona"),
                priority=m.get("priority", 50), time_velocity=m.get("time_velocity", "static"),
                ttl_days=None, dimension_ids=m.get("dimension_ids", []),
            )
            stored += 1
        return {"stored": stored, "skipped": 0, "updated": 0, "merged": 0,
                "archived": 0, "l15_error": None, "fallback": False,
                "supersession_rejected": 0}

    monkeypatch.setattr(pipeline_mod, "persist_memories", flaky)


def test_t223_persist_failure_keeps_new_and_retry_recovers(conns, raw_dir, cfg, monkeypatch):
    """故障注入 → 文件保持 new / 0 记忆（RED：旧行为是 refined）；重试重新提取并落库。"""
    mem_conn, session_conn = conns
    fid = _make_raw_file(session_conn, file_id="f-retry", last_refined_seq=1)
    l1_calls: list = []
    _stub_l1_one_memory(monkeypatch, l1_calls)
    calls: dict = {}
    _flaky_persist(monkeypatch, calls, fail_times=1)

    # 第一次：落库崩溃 → 异常上抛，游标必须原地不动
    with pytest.raises(RuntimeError, match="injected persist failure"):
        pipeline_mod.refine_one(fid, mem_conn, session_conn, cfg)

    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "new"            # ← RED 断言点（旧行为：refined）
    assert rf["last_refined_seq"] == 1      # ← 游标未推进
    assert not rf["content_hash"]
    assert mem_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    # 第二次（重试）：重新提取 + 落库成功 → 游标推进
    result, stats = pipeline_mod.refine_one(fid, mem_conn, session_conn, cfg)

    assert len(l1_calls) == 2, "重试必须重新提取（不是走无增量分支）"
    assert result.status == "refined"
    assert stats["stored"] == 1
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "refined"
    assert rf["last_refined_seq"] == 2
    assert len(rf["content_hash"]) == 64
    assert _rows(mem_conn) == ["用户使用 Python 3.11"]


def test_t223_batch_scan_repicks_after_persist_failure(conns, raw_dir, cfg, monkeypatch):
    """批扫兜底：首轮 persist 故障不标记 refined → 下一轮重扫同一文件并落库。"""
    mem_conn, session_conn = conns
    fid = _make_raw_file(session_conn, file_id="f-scan-retry", last_refined_seq=1)
    l1_calls: list = []
    _stub_l1_one_memory(monkeypatch, l1_calls)
    calls: dict = {}
    _flaky_persist(monkeypatch, calls, fail_times=1)

    r1 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg, max_files=10)

    assert r1["scanned"] == 1 and r1["failed"] == 1
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "new" and rf["last_refined_seq"] == 1
    assert mem_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    r2 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg, max_files=10)

    assert r2["scanned"] == 1 and r2["refined"] == 1
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "refined" and rf["last_refined_seq"] == 2
    assert _rows(mem_conn) == ["用户使用 Python 3.11"]
