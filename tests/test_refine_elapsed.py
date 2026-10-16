"""ST-58/T-289：`refine_runs` 真实耗时（`elapsed_ms`）+ 悬挂 running 行收敛。

背景：三处 run 记账原先都在 LLM 调用**之后**才 `start()`，导致全表
`started_at == finished_at`（生产 21,857 行无一例外），效率完全不可度量。
T-289 把 `start()` 前移到 LLM 调用之前，并新增 `elapsed_ms` 列承载真实墙钟耗时。

口径约定（防各处漂移）：`elapsed_ms` **由 recorder 依 started_at 计算**，调用点
不传值；`provider` 因 start 时降级链尚未跑完，起手写 "pending" 由 finish 回填。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sgme.data import db as db_mod
from sgme.data.refine_dao import RefineRunRecorder, STALE_RUNNING_SECONDS


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk_conn(tmp_path):
    return db_mod.connect_memory(tmp_path)


# ---------- 1. 列存在且可空（迁移幂等） ----------


def test_elapsed_ms_column_exists(tmp_path):
    """新库走 DDL、老库走迁移，两种路径都应有 elapsed_ms 列。"""
    conn = _mk_conn(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(refine_runs)").fetchall()]
    assert "elapsed_ms" in cols


def test_migration_idempotent(tmp_path):
    """迁移函数重复执行安全（启动时每次都跑）。"""
    conn = _mk_conn(tmp_path)
    db_mod._migrate_refine_runs_elapsed_ms(conn)
    db_mod._migrate_refine_runs_elapsed_ms(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(refine_runs)").fetchall()]
    assert cols.count("elapsed_ms") == 1


def test_migration_adds_column_to_old_schema(tmp_path):
    """老库补列：模拟没有 elapsed_ms 的旧表，迁移后必须补上（生产真实路径）。"""
    import sqlite3

    old_db = tmp_path / "old_memory.db"
    con = sqlite3.connect(str(old_db))
    # 复刻「当前生产库减去 elapsed_ms」的形态（candidate_count 三列是 2026-10-09
    # 已迁移到位的，真实老库一定已有；这里只缺本次要补的 elapsed_ms）
    con.execute(
        """
        CREATE TABLE refine_runs (
          run_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, stage TEXT NOT NULL,
          version TEXT NOT NULL, variant TEXT, provider TEXT NOT NULL,
          bucket_key TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
          memories_count INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER,
          completion_tokens INTEGER, total_tokens INTEGER,
          action_counts TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'running', error TEXT,
          candidate_count INTEGER, candidate_ids_hash TEXT, short_circuit_reason TEXT)
        """
    )
    con.commit()
    before = [r[1] for r in con.execute("PRAGMA table_info(refine_runs)").fetchall()]
    assert "elapsed_ms" not in before

    db_mod._migrate_refine_runs_elapsed_ms(con)
    after = [r[1] for r in con.execute("PRAGMA table_info(refine_runs)").fetchall()]
    assert "elapsed_ms" in after

    # 补列后可正常记账，且老行保持可空
    run_id = RefineRunRecorder.start(
        con, file_id="f1", stage="l1_extraction", version="v1",
        variant=None, provider="pending", bucket_key="b1",
    )
    RefineRunRecorder.finish(con, run_id, memories_count=1, action_counts={}, status="ok")
    row = con.execute(
        "SELECT elapsed_ms, provider FROM refine_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row[0] is not None and row[0] >= 0
    con.close()


def test_elapsed_ms_nullable_for_old_rows(tmp_path):
    """老行无值不影响读取（可空列，旧代码可安全运行）。"""
    conn = _mk_conn(tmp_path)
    conn.execute(
        "INSERT INTO refine_runs (run_id, file_id, stage, version, provider, "
        "bucket_key, started_at, status) VALUES ('r-old','f','l1_extraction','v',"
        "'agnes','b',?,'ok')",
        (_iso(datetime.now(timezone.utc)),),
    )
    conn.commit()
    row = conn.execute(
        "SELECT elapsed_ms FROM refine_runs WHERE run_id='r-old'"
    ).fetchone()
    assert row[0] is None


# ---------- 2. elapsed_ms 依 started_at 计算 ----------


def test_elapsed_ms_positive(tmp_path):
    """真实耗时 > 0（修复前恒为 0，因为 start 在 LLM 返回之后）。"""
    conn = _mk_conn(tmp_path)
    run_id = RefineRunRecorder.start(
        conn, file_id="f1", stage="l1_extraction", version="v1",
        variant=None, provider="pending", bucket_key="b1",
    )
    time.sleep(0.01)
    RefineRunRecorder.finish(
        conn, run_id, memories_count=1, action_counts={}, status="ok",
    )
    row = conn.execute(
        "SELECT elapsed_ms FROM refine_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row[0] is not None and row[0] > 0, f"elapsed_ms 应 > 0，实际 {row[0]}"


def test_elapsed_ms_derived_from_started_at(tmp_path):
    """口径锚定：耗时由 started_at 推出，不是由调用点传入。"""
    conn = _mk_conn(tmp_path)
    run_id = RefineRunRecorder.start(
        conn, file_id="f1", stage="l1_conflict", version="v1",
        variant=None, provider="pending", bucket_key="b1",
    )
    # 把 started_at 拨回 3 秒前
    past = datetime.now(timezone.utc) - timedelta(seconds=3)
    conn.execute(
        "UPDATE refine_runs SET started_at=? WHERE run_id=?", (_iso(past), run_id)
    )
    conn.commit()
    RefineRunRecorder.finish(
        conn, run_id, memories_count=0, action_counts={}, status="ok",
    )
    row = conn.execute(
        "SELECT elapsed_ms FROM refine_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row[0] is not None and row[0] >= 2900, f"应约 3000ms，实际 {row[0]}"


def test_elapsed_ms_null_when_started_at_missing(tmp_path):
    """run_id 不存在 / started_at 缺失 → elapsed_ms 写 NULL，且不抛错。"""
    conn = _mk_conn(tmp_path)
    # 不存在的 run_id：finish 不得抛异常
    RefineRunRecorder.finish(
        conn, "no-such-run", memories_count=0, action_counts={}, status="error",
    )
    row = conn.execute(
        "SELECT elapsed_ms FROM refine_runs WHERE run_id='no-such-run'"
    ).fetchone()
    assert row is None  # 行本就不存在，UPDATE 只影响 0 行


# ---------- 3. provider 回填（COALESCE 语义） ----------


def test_provider_backfilled_by_finish(tmp_path):
    """start 写 pending → finish 传 provider 回填。"""
    conn = _mk_conn(tmp_path)
    run_id = RefineRunRecorder.start(
        conn, file_id="f1", stage="l1_extraction", version="v1",
        variant=None, provider="pending", bucket_key="b1",
    )
    RefineRunRecorder.finish(
        conn, run_id, memories_count=1, action_counts={}, status="ok",
        provider="agnes",
    )
    row = conn.execute(
        "SELECT provider FROM refine_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row[0] == "agnes"


def test_provider_kept_when_not_passed(tmp_path):
    """finish 不传 provider（老调用点）→ 保持 start 时的值，不被抹掉。"""
    conn = _mk_conn(tmp_path)
    run_id = RefineRunRecorder.start(
        conn, file_id="f1", stage="l2_scene", version="v1",
        variant=None, provider="siliconflow", bucket_key="b1",
    )
    RefineRunRecorder.finish(
        conn, run_id, memories_count=1, action_counts={}, status="ok",
    )
    row = conn.execute(
        "SELECT provider FROM refine_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row[0] == "siliconflow"


# ---------- 4. 悬挂 running 行收敛 ----------


def _insert_running(conn, run_id: str, age_seconds: int) -> None:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    conn.execute(
        "INSERT INTO refine_runs (run_id, file_id, stage, version, provider, "
        "bucket_key, started_at, status) VALUES (?,'f','l1_conflict','v','pending',"
        "'b',?,'running')",
        (run_id, _iso(ts)),
    )
    conn.commit()


def test_reap_stale_only(tmp_path):
    """只收敛超期行，在途（新鲜）running 行不得误伤。"""
    conn = _mk_conn(tmp_path)
    _insert_running(conn, "r-stale", STALE_RUNNING_SECONDS + 60)
    _insert_running(conn, "r-fresh", 5)

    n = RefineRunRecorder.reap_stale_running(conn, publish_anomaly=False)
    assert n == 1

    stale = conn.execute(
        "SELECT status FROM refine_runs WHERE run_id='r-stale'"
    ).fetchone()
    fresh = conn.execute(
        "SELECT status FROM refine_runs WHERE run_id='r-fresh'"
    ).fetchone()
    assert stale[0] == "error"
    assert fresh[0] == "running", "在途批次不得被误伤"


def test_reap_idempotent(tmp_path):
    conn = _mk_conn(tmp_path)
    _insert_running(conn, "r-stale", STALE_RUNNING_SECONDS + 60)
    assert RefineRunRecorder.reap_stale_running(conn, publish_anomaly=False) == 1
    assert RefineRunRecorder.reap_stale_running(conn, publish_anomaly=False) == 0


def test_reap_no_finished_at_fabrication(tmp_path):
    """收敛只改 status + error，不补 finished_at（保留「未正常收尾」痕迹）。"""
    conn = _mk_conn(tmp_path)
    _insert_running(conn, "r-stale", STALE_RUNNING_SECONDS + 60)
    RefineRunRecorder.reap_stale_running(conn, publish_anomaly=False)
    row = conn.execute(
        "SELECT finished_at, elapsed_ms FROM refine_runs WHERE run_id='r-stale'"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_reap_publish_failure_is_nonblocking(tmp_path):
    """告警发布失败不得影响收敛结果（照 edge_dao 先例）。"""
    conn = _mk_conn(tmp_path)
    _insert_running(conn, "r-stale", STALE_RUNNING_SECONDS + 60)
    # 不注入 signal engine（真实环境可能未接线）→ publish 内部异常应被吞掉
    n = RefineRunRecorder.reap_stale_running(conn, publish_anomaly=True)
    assert n == 1
