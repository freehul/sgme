"""接线回归：refine_dao.finish 收尾后必须把 LLM 用量累加进 llm_usage_daily。

背景（设计文档第 12 项 / D-63）：成本账本此前只有 DAO 与回填工具，没有任何调用点
把「实时提炼」的用量写进去，导致上线后告警无数据。本用例锁住这条接线——
去掉 refine_dao.finish 里的记账分支，本用例即红。

边界：记账是**旁路**——它失败不得影响提炼结果落库，故只断言正向落库与聚合累加。
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.refine_dao import RefineRunRecorder


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    from sgme import config

    cfg = config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _run_once(conn: sqlite3.Connection, provider: str, prompt_tokens: int, completion_tokens: int) -> str:
    """跑一次 start→finish，返回 run_id。"""
    run_id = RefineRunRecorder.start(
        conn,
        file_id="f-0001",
        stage="l1_extraction",
        version="v019",
        variant=None,
        provider=provider,
        bucket_key="b-1",
    )
    RefineRunRecorder.finish(
        conn,
        run_id,
        memories_count=3,
        action_counts={"extracted": 3},
        status="ok",
        usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
               "total_tokens": prompt_tokens + completion_tokens},
        provider=provider,
    )
    return run_id


def _usage_rows(conn: sqlite3.Connection) -> list[tuple]:
    # 账本表是懒建的（首次记账才建），无记账时表不存在 = 空台账
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_usage_daily'"
    ).fetchone()
    if not exists:
        return []
    return conn.execute(
        "SELECT day, provider, model, stage, prompt_tokens, completion_tokens, calls "
        "FROM llm_usage_daily ORDER BY day, provider, model, stage"
    ).fetchall()


def test_finish_writes_usage_row(mem_conn):
    """收尾一次 → 账本出现一行，token 数与批次一致。"""
    _run_once(mem_conn, "magpie", 1200, 340)
    rows = _usage_rows(mem_conn)
    assert len(rows) == 1, f"应恰好一行台账，实际 {rows}"
    day, provider, model, stage, pt, ct, calls = rows[0]
    assert provider == "magpie"
    assert stage == "l1_extraction"
    assert (pt, ct, calls) == (1200, 340, 1)


def test_finish_accumulates_same_bucket(mem_conn):
    """同 (day, provider, model, stage) 两次收尾 → 同一行累加，不新增行。"""
    _run_once(mem_conn, "magpie", 1000, 200)
    _run_once(mem_conn, "magpie", 500, 100)
    rows = _usage_rows(mem_conn)
    assert len(rows) == 1, f"同桶应聚合为一行，实际 {rows}"
    _, _, _, _, pt, ct, calls = rows[0]
    assert (pt, ct, calls) == (1500, 300, 2)


def test_finish_without_usage_writes_nothing(mem_conn):
    """usage 为 None（短路径/规则兜底批次）→ 不写账本，但不报错。"""
    run_id = RefineRunRecorder.start(
        mem_conn,
        file_id="f-0002",
        stage="l1_extraction",
        version="v019",
        variant=None,
        provider="rule",
        bucket_key="b-2",
    )
    RefineRunRecorder.finish(
        mem_conn, run_id, memories_count=0, action_counts={}, status="ok", provider="rule"
    )
    assert _usage_rows(mem_conn) == []
    # 主记账不受影响：批次本身仍正常收尾
    row = mem_conn.execute(
        "SELECT status, memories_count FROM refine_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert (row[0], row[1]) == ("ok", 0)
