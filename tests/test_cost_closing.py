"""T-421 · D-63 成本计量三缺口收口测试（2026-11-18）。

三缺口：
1. backfill 入口（脚本默认 dry-run / --apply 才写；幂等）
2. 04:00 汇总（cost_scheduler 守护定时器，D-7 口径不引 APScheduler）
3. 停摆开关 cost.circuit_breaker.enabled（默认 false；开 = 跳过提炼 + 告警）

验收锁死：回填幂等 / dry-run 零写入 / 汇总产出当日行 / 开关关不拦 / 开关开跳过提炼 /
调度异常不炸线程。tmp_path 隔离。
"""
from __future__ import annotations

import threading
import time

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.engine import cost_alert, cost_meter
from sgme.engine import cost_scheduler


# ---------- fixtures ----------

@pytest.fixture
def mem_conn(tmp_path):
    """memory.db 连接（refine_runs / 账本所在库）。"""
    conn = db_mod.connect_memory(tmp_path / "data")
    yield conn
    try:
        conn.close()
    except Exception:
        pass


@pytest.fixture
def session_conn(tmp_path):
    """session.db 连接（raw_files 所在库；与 mem_conn 同一 data_dir）。"""
    conn = db_mod.connect_session(tmp_path / "data")
    yield conn
    try:
        conn.close()
    except Exception:
        pass


def _seed_run(conn, run_id: str, day: str = "2026-11-18", tokens: int = 1000) -> None:
    """造一行已收尾的 refine_runs 批次（bucket_key/version 非空约束给占位值）。"""
    conn.execute(
        "INSERT INTO refine_runs (run_id, file_id, stage, version, provider, bucket_key, "
        "started_at, finished_at, prompt_tokens, completion_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, f"f-{run_id}", "l1_extraction", "v0", "agnes", "b1",
         f"{day}T01:00:00Z", f"{day}T02:00:00Z", tokens, 100),
    )
    conn.commit()


# ---------- 缺口①：backfill 入口 ----------

def test_backfill_idempotent(mem_conn):
    """回填幂等：跑两次第二次 rows=0，账本 calls 不翻倍（游标表挡重放）。"""
    _seed_run(mem_conn, "r1")
    first = cost_meter.backfill_from_refine_runs(mem_conn)
    assert first["rows"] == 1
    row = mem_conn.execute("SELECT calls FROM llm_usage_daily").fetchone()
    assert int(row["calls"]) == 1
    second = cost_meter.backfill_from_refine_runs(mem_conn)
    assert second["rows"] == 0  # 游标表挡住重放
    row = mem_conn.execute("SELECT calls FROM llm_usage_daily").fetchone()
    assert int(row["calls"]) == 1  # 不双倍计费


def test_pending_report_dry_run_no_write(mem_conn):
    """dry-run 报告函数只读：返回待回填数；账本零行、游标表不建（零写入）。"""
    _seed_run(mem_conn, "r2")
    out = cost_meter.pending_report(mem_conn)
    assert out["pending_runs"] == 1
    # 账本表由 DDL 预建（connect_memory 即存在），dry-run 后必须仍是空表
    n = mem_conn.execute("SELECT COUNT(*) FROM llm_usage_daily").fetchone()[0]
    assert int(n) == 0
    tables = {
        r[0] for r in mem_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "llm_usage_backfill" not in tables  # 游标表由 backfill 懒建


# ---------- 缺口②：04:00 汇总 ----------

def test_summary_produces_day_rows(mem_conn):
    """汇总任务：refine_runs → 账本当日行（backfill + 告警评估一次跑完）。"""
    _seed_run(mem_conn, "r3")
    cfg = {"cost": {"summary": {"enabled": True, "schedule": "04:00"}}}
    out = cost_scheduler.run_cost_summary(cfg, mem_conn)
    assert out["backfilled_days"] >= 1
    row = mem_conn.execute(
        "SELECT prompt_tokens, calls FROM llm_usage_daily WHERE day='2026-11-18'").fetchone()
    assert row is not None
    assert int(row["prompt_tokens"]) == 1000
    assert "budget" in out  # 告警评估结果带出（available/level）


def test_scheduler_loop_runs_and_survives(mem_conn, monkeypatch):
    """定时器循环：到点触发汇总；汇总内部抛异常不炸线程（下一轮继续）。"""
    calls = {"n": 0}

    def flaky(cfg, conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟账本写入异常")
        return {"backfilled_days": 0, "rows": 0, "budget": {"available": False}}

    monkeypatch.setattr(cost_scheduler, "run_cost_summary", flaky)
    monkeypatch.setattr(cost_scheduler, "_seconds_until", lambda s: 0.05)
    stop = threading.Event()
    t = threading.Thread(target=cost_scheduler._scheduler_loop,
                         args=({"cost": {"summary": {"enabled": True}}}, stop, None),
                         daemon=True)
    t.start()
    deadline = time.time() + 5
    while calls["n"] < 2 and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    t.join(timeout=3)
    assert not t.is_alive()   # 异常后线程仍活着并能继续下一轮
    assert calls["n"] >= 2    # 第一轮炸了第二轮照跑


def test_scheduler_disabled_skips(mem_conn, monkeypatch):
    """cost.summary.enabled=false（新开关默认 false）→ 到点只跳过不执行。"""
    ran = {"n": 0}
    monkeypatch.setattr(cost_scheduler, "run_cost_summary",
                        lambda cfg, conn: ran.__setitem__("n", ran["n"] + 1) or {})
    monkeypatch.setattr(cost_scheduler, "_seconds_until", lambda s: 0.05)
    stop = threading.Event()
    t = threading.Thread(target=cost_scheduler._scheduler_loop,
                         args=({"cost": {"summary": {"enabled": False}}}, stop, None),
                         daemon=True)
    t.start()
    time.sleep(0.4)
    stop.set()
    t.join(timeout=3)
    assert ran["n"] == 0  # 开关关：汇总不执行


# ---------- 缺口③：停摆开关 ----------

def test_breaker_default_off_no_block():
    """cost.circuit_breaker 缺省（false）→ 不拦提炼。"""
    assert cost_alert.is_refine_blocked({}) is False
    assert cost_alert.is_refine_blocked({"cost": {"circuit_breaker": {"enabled": False}}}) is False


def test_breaker_on_skips_refine(mem_conn, session_conn):
    """开关开 → refine_file 直接 deferred（文件保持 new，不调 LLM、不改状态）。

    deferred 返回发生在体积闸/LLM 调用之前（同 is_paused 挂点），故不 mock LLM
    也不会触达调用——若熔断挂点失效，本用例会因 L0 文件不存在走进 error 分支而暴露。
    """
    from sgme.engine import refine as refine_mod

    _seed_run(mem_conn, "unused")
    cfg = {"cost": {"circuit_breaker": {"enabled": True}}}
    session_conn.execute(
        "INSERT INTO raw_files (file_id, path, session_key, status) "
        "VALUES ('f-break', 'sessions/f-break.md', 's-break', 'new')")
    session_conn.commit()
    result = refine_mod.refine_file("f-break", mem_conn, session_conn, cfg)
    assert result.deferred is True
    assert result.error == "cost_circuit_breaker"
    row = session_conn.execute(
        "SELECT status, fail_count FROM raw_files WHERE file_id='f-break'").fetchone()
    assert row["status"] == "new" and int(row["fail_count"]) == 0  # 状态与熔断计数不动


def test_breaker_on_batch_scan_round_skip(mem_conn):
    """开关开 → batch_scan 整轮跳过（连队列都不扫），状态 blocked_by_cost。"""
    from sgme.engine import batch_scan

    cfg = {"cost": {"circuit_breaker": {"enabled": True}}}
    out = batch_scan.run_batch_scan(mem_conn, mem_conn, cfg, 5)
    assert out["status"] == "blocked_by_cost"
    assert out["scanned"] == 0
