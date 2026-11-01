"""tests/test_cost_meter.py：LLM 用量按天成本计量（设计第12项 / D-63）。

覆盖：
1. llm_usage_dao：建表幂等 / record upsert 累加 / 跨天分桶 /
   daily_summary 与 total_by_model 的空数据、窗口与排序 / 按 model 汇总
2. cost_meter.extract_from_refine_run：正常抽取 / finished_at 为 None 跳过 /
   model 缺列回退 provider / NULL tokens 归零
3. cost_meter.backfill_from_refine_runs：聚合正确并跳过未收尾行 /
   幂等（跑两次 calls 不翻倍）/ limit 分批续填 / 空数据返回零
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sgme.data import db as db_mod
from sgme.data import llm_usage_dao
from sgme.engine import cost_meter


# ---------- fixtures ----------


@pytest.fixture
def mem_conn(tmp_path):
    """真实三库初始化（refine_runs 用真 schema），并建好账本表。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    llm_usage_dao.ensure_table(mem_conn)
    yield mem_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def _ledger_row(
    mem_conn,
    day,
    provider="agnes",
    model="agnes-2.5-flash",
    stage="l1_extraction",
):
    """取账本单桶行；无则 None。"""
    row = mem_conn.execute(
        "SELECT * FROM llm_usage_daily "
        "WHERE day=? AND provider=? AND model=? AND stage=?",
        (day, provider, model, stage),
    ).fetchone()
    return dict(row) if row else None


def _insert_run(
    mem_conn,
    run_id,
    finished_at,
    provider="agnes",
    stage="l1_extraction",
    prompt_tokens=100,
    completion_tokens=50,
):
    """造一条 refine_runs 行（finished_at=None 表示未收尾批次）。"""
    mem_conn.execute(
        """
        INSERT INTO refine_runs
          (run_id, file_id, stage, version, provider, bucket_key,
           started_at, finished_at, memories_count, status,
           prompt_tokens, completion_tokens, total_tokens)
        VALUES (?,?,?,?,?,?,?,?,?,'ok',?,?,?)
        """,
        (run_id, f"file-{run_id}", stage, "v001", provider, "b1",
         finished_at or "2026-10-30T07:00:00Z", finished_at, 1,
         prompt_tokens, completion_tokens,
         (prompt_tokens or 0) + (completion_tokens or 0)),
    )
    mem_conn.commit()


# ---------- 1. llm_usage_dao ----------


def test_ensure_table_idempotent(mem_conn):
    # 第二次调用不抛错（幂等建表）
    llm_usage_dao.ensure_table(mem_conn)
    cols = {r["name"] for r in mem_conn.execute("PRAGMA table_info(llm_usage_daily)")}
    assert {"day", "provider", "model", "stage",
            "prompt_tokens", "completion_tokens", "calls"} <= cols


def test_record_usage_accumulates_same_bucket(mem_conn):
    llm_usage_dao.record_usage(
        mem_conn, "2026-10-30", "agnes", "agnes-2.5-flash", "l1_extraction", 100, 50)
    llm_usage_dao.record_usage(
        mem_conn, "2026-10-30", "agnes", "agnes-2.5-flash", "l1_extraction", 30, 20)
    row = _ledger_row(mem_conn, "2026-10-30")
    assert row["calls"] == 2
    assert row["prompt_tokens"] == 130
    assert row["completion_tokens"] == 70
    # 同桶只有一行
    assert mem_conn.execute("SELECT COUNT(*) FROM llm_usage_daily").fetchone()[0] == 1


def test_record_usage_buckets_by_day(mem_conn):
    llm_usage_dao.record_usage(
        mem_conn, "2026-10-30", "agnes", "agnes-2.5-flash", "l1_extraction", 100, 50)
    llm_usage_dao.record_usage(
        mem_conn, "2026-10-31", "agnes", "agnes-2.5-flash", "l1_extraction", 100, 50)
    rows = mem_conn.execute(
        "SELECT day, calls FROM llm_usage_daily ORDER BY day").fetchall()
    assert [dict(r) for r in rows] == [
        {"day": "2026-10-30", "calls": 1},
        {"day": "2026-10-31", "calls": 1},
    ]


def test_daily_summary_and_total_by_model_empty(mem_conn):
    # 空账本：两个查询都返回空列表，不抛错
    assert llm_usage_dao.daily_summary(mem_conn) == []
    assert llm_usage_dao.total_by_model(mem_conn) == []


def test_daily_summary_orders_by_day_desc_and_limits_window(mem_conn):
    now = datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    old = (now - timedelta(days=40)).strftime("%Y-%m-%d")
    for day in (old, today, yesterday):  # 故意乱序写入，验证排序来自查询
        llm_usage_dao.record_usage(
            mem_conn, day, "agnes", "agnes-2.5-flash", "l1_extraction", 10, 5)
    rows = llm_usage_dao.daily_summary(mem_conn, days=30)
    # 40 天前的行被窗口排除；剩余按 day DESC
    assert [r["day"] for r in rows] == [today, yesterday]
    assert rows[0]["calls"] == 1
    assert rows[0]["prompt_tokens"] == 10


def test_total_by_model_groups_and_sums(mem_conn):
    llm_usage_dao.record_usage(
        mem_conn, "2026-10-30", "agnes", "agnes-2.5-flash", "l1_extraction", 100, 50)
    llm_usage_dao.record_usage(
        mem_conn, "2026-10-31", "agnes", "agnes-2.5-flash", "l1_conflict", 10, 5)
    llm_usage_dao.record_usage(
        mem_conn, "2026-10-30", "siliconflow", "THUDM/GLM-4-9B-0414",
        "l1_extraction", 7, 3)
    by_model = {r["model"]: r for r in llm_usage_dao.total_by_model(mem_conn)}
    assert set(by_model) == {"agnes-2.5-flash", "THUDM/GLM-4-9B-0414"}
    assert by_model["agnes-2.5-flash"]["calls"] == 2
    assert by_model["agnes-2.5-flash"]["prompt_tokens"] == 110
    assert by_model["agnes-2.5-flash"]["completion_tokens"] == 55
    assert by_model["THUDM/GLM-4-9B-0414"]["calls"] == 1


# ---------- 2. extract_from_refine_run ----------


def test_extract_normal_run_row():
    row = {"provider": "agnes", "model": "agnes-2.5-flash", "stage": "l1_extraction",
           "prompt_tokens": 100, "completion_tokens": 50,
           "finished_at": "2026-10-30T08:15:00Z"}
    assert cost_meter.extract_from_refine_run(row) == {
        "day": "2026-10-30",
        "provider": "agnes",
        "model": "agnes-2.5-flash",
        "stage": "l1_extraction",
        "prompt_tokens": 100,
        "completion_tokens": 50,
    }


def test_extract_returns_none_when_finished_at_missing():
    assert cost_meter.extract_from_refine_run(
        {"provider": "agnes", "stage": "l1_extraction",
         "prompt_tokens": 1, "completion_tokens": 1, "finished_at": None},
    ) is None
    assert cost_meter.extract_from_refine_run({}) is None


def test_extract_model_falls_back_to_provider_and_null_tokens_to_zero():
    # refine_runs 无 model 列：回退 provider；NULL tokens 归 0 不丢调用
    row = {"provider": "siliconflow", "stage": "l1_extraction",
           "prompt_tokens": None, "completion_tokens": None,
           "finished_at": "2026-10-31T00:00:00Z"}
    tup = cost_meter.extract_from_refine_run(row)
    assert tup["model"] == "siliconflow"
    assert tup["prompt_tokens"] == 0
    assert tup["completion_tokens"] == 0


# ---------- 3. backfill_from_refine_runs ----------


def test_backfill_aggregates_and_skips_unfinished(mem_conn):
    _insert_run(mem_conn, "r1", "2026-10-30T08:00:00Z",
                prompt_tokens=100, completion_tokens=50)
    _insert_run(mem_conn, "r2", "2026-10-30T09:00:00Z",
                prompt_tokens=40, completion_tokens=10)
    _insert_run(mem_conn, "r3", "2026-10-31T08:00:00Z", provider="siliconflow",
                stage="l1_conflict", prompt_tokens=7, completion_tokens=3)
    _insert_run(mem_conn, "r4", None)  # 未收尾（finished_at=NULL）：必须跳过

    result = cost_meter.backfill_from_refine_runs(mem_conn)
    assert result == {"backfilled_days": 2, "rows": 3}

    # refine_runs 无 model 列：回填按 model=provider 分桶
    d30 = _ledger_row(mem_conn, "2026-10-30", model="agnes")
    assert d30["calls"] == 2
    assert d30["prompt_tokens"] == 140
    assert d30["completion_tokens"] == 60
    # 无 model 列：回填按 model=provider 分桶
    d31 = _ledger_row(mem_conn, "2026-10-31", provider="siliconflow",
                      model="siliconflow", stage="l1_conflict")
    assert d31["calls"] == 1
    assert d31["prompt_tokens"] == 7
    assert d31["completion_tokens"] == 3
    assert _ledger_row(mem_conn, "2026-10-31", model="agnes") is None


def test_backfill_is_idempotent_calls_not_doubled(mem_conn):
    _insert_run(mem_conn, "r1", "2026-10-30T08:00:00Z",
                prompt_tokens=100, completion_tokens=50)
    first = cost_meter.backfill_from_refine_runs(mem_conn)
    assert first == {"backfilled_days": 1, "rows": 1}
    # 第二次回填：不再累计
    second = cost_meter.backfill_from_refine_runs(mem_conn)
    assert second == {"backfilled_days": 0, "rows": 0}
    row = _ledger_row(mem_conn, "2026-10-30", model="agnes")
    assert row["calls"] == 1
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 50


def test_backfill_limit_processes_in_batches(mem_conn):
    _insert_run(mem_conn, "r1", "2026-10-30T08:00:00Z")
    _insert_run(mem_conn, "r2", "2026-10-31T08:00:00Z")
    # limit=1 每次只吃一条，重跑续填剩余
    assert cost_meter.backfill_from_refine_runs(mem_conn, limit=1) == {
        "backfilled_days": 1, "rows": 1}
    assert cost_meter.backfill_from_refine_runs(mem_conn, limit=1) == {
        "backfilled_days": 1, "rows": 1}
    assert cost_meter.backfill_from_refine_runs(mem_conn, limit=1) == {
        "backfilled_days": 0, "rows": 0}
    total_calls = mem_conn.execute(
        "SELECT SUM(calls) FROM llm_usage_daily").fetchone()[0]
    assert total_calls == 2


def test_backfill_empty_source_returns_zero(mem_conn):
    assert cost_meter.backfill_from_refine_runs(mem_conn) == {
        "backfilled_days": 0, "rows": 0}
