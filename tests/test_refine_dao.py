"""#33 测试：refine_runs 审计 DAO + v3 迁移（prompt_version 列）。

覆盖：
- RefineRunRecorder start/finish（ok / error / action 分布）
- list_by_stage（since 过滤）
- summarize 按 (version, variant) 分组（runs / error_runs / memories_count / action_dist / avg_priority）
- SCHEMA_VERSION == 4；老库迁移幂等（无 prompt_version 列 → ALTER 补列，重复执行安全）
- insert_memory(prompt_version=...) 向后兼容（缺省 None）
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.refine_dao import RefineRunRecorder


# ---------- fixtures ----------

@pytest.fixture
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    # 导入注册表（memory_tags 外键需要 dimension_registry 行）
    from sgme import config
    cfg = config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


# ---------- schema v4 ----------

def test_schema_version_is_current(mem_conn):
    """SCHEMA_VERSION 由 db 常量定义（v6：D-56 补 superseded_by）；不得在测试里写死。"""
    assert db_mod.SCHEMA_VERSION >= 6
    assert db_mod.schema_version(mem_conn) == db_mod.SCHEMA_VERSION


def test_refine_runs_table_exists(mem_conn):
    """refine_runs 表 + 索引已建。"""
    tables = set(db_mod.list_tables(mem_conn))
    assert "refine_runs" in tables
    idx = {r["name"] for r in mem_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()}
    assert "idx_refine_runs_stage" in idx
    assert "idx_refine_runs_file" in idx


def test_memories_has_prompt_version_column(mem_conn):
    """memories / memory_archive 均含 prompt_version 列。"""
    for table in ("memories", "memory_archive"):
        cols = [r[1] for r in mem_conn.execute(f"PRAGMA table_info({table})").fetchall()]
        assert "prompt_version" in cols, f"{table} 缺 prompt_version 列"


def test_legacy_db_migration_adds_prompt_version(tmp_path):
    """老库（无 prompt_version 列）→ connect_memory 自动 ALTER 补列（幂等）。"""
    # 手工建 v2 形态老库：memories/memory_archive 无 prompt_version 列 + schema_versions 记 v2
    p = tmp_path / "memory.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(
        """
        CREATE TABLE memories (
          memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, memory_type TEXT NOT NULL,
          priority INTEGER NOT NULL, time_velocity TEXT NOT NULL, ttl_days INTEGER,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, agent_tag TEXT);
        CREATE TABLE memory_archive (
          memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, memory_type TEXT NOT NULL,
          priority INTEGER NOT NULL, time_velocity TEXT NOT NULL, ttl_days INTEGER,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, agent_tag TEXT,
          archived_at TEXT NOT NULL, superseded_by TEXT);
        CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
        INSERT INTO schema_versions VALUES (2, 'memory_v2', '2026-08-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    # 老库升级：connect_memory 补列 + 登记 v4（不丢数据）
    upgraded = db_mod.connect_memory(tmp_path)
    try:
        cols = [r[1] for r in upgraded.execute("PRAGMA table_info(memories)").fetchall()]
        assert "prompt_version" in cols
        cols_arch = [r[1] for r in upgraded.execute("PRAGMA table_info(memory_archive)").fetchall()]
        assert "prompt_version" in cols_arch
        assert db_mod.schema_version(upgraded) == db_mod.SCHEMA_VERSION
        # 老数据行保留
        upgraded.execute(
            "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, created_at, updated_at) "
            "VALUES ('m1','旧数据','persona',50,'static','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
        upgraded.commit()
        # 重复迁移（幂等）不报错、不丢数据
        db_mod._migrate_mem_prompt_version(upgraded)
        db_mod._migrate_mem_prompt_version(upgraded)
        row = upgraded.execute("SELECT * FROM memories WHERE memory_id='m1'").fetchone()
        assert row["content"] == "旧数据"
        assert row["prompt_version"] is None
    finally:
        upgraded.close()


# ---------- start / finish ----------

def test_start_finish_ok(mem_conn):
    """start → finish(ok)：一条完整 run 记录。"""
    run_id = RefineRunRecorder.start(
        mem_conn, file_id="f1", stage="l1_extraction",
        version="v002", variant="A", provider="lm-studio", bucket_key="f1",
    )
    assert run_id
    RefineRunRecorder.finish(mem_conn, run_id, memories_count=3, action_counts={}, status="ok")
    runs = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert len(runs) == 1
    r = runs[0]
    assert r["run_id"] == run_id
    assert r["stage"] == "l1_extraction"
    assert r["version"] == "v002"
    assert r["variant"] == "A"
    assert r["provider"] == "lm-studio"
    assert r["bucket_key"] == "f1"
    assert r["memories_count"] == 3
    assert r["status"] == "ok"
    assert r["error"] is None
    assert r["finished_at"] is not None


def test_start_finish_error(mem_conn):
    """finish(error) → status=error + error 文本。"""
    run_id = RefineRunRecorder.start(
        mem_conn, file_id="f1", stage="l1_conflict",
        version="working-abc12345", variant=None, provider="deepseek", bucket_key="f1",
    )
    RefineRunRecorder.finish(
        mem_conn, run_id, memories_count=0, action_counts={},
        status="error", error="LLM 输出解析失败",
    )
    runs = RefineRunRecorder.list_by_stage(mem_conn, "l1_conflict")
    assert runs[0]["status"] == "error"
    assert runs[0]["error"] == "LLM 输出解析失败"


def test_list_by_stage_since_filter(mem_conn):
    """list_by_stage 支持 since 过滤。"""
    r1 = RefineRunRecorder.start(mem_conn, "f1", "l2_scene", "v001", None, "lm-studio", "f1")
    RefineRunRecorder.finish(mem_conn, r1, 1, {"create": 1}, "ok")
    r2 = RefineRunRecorder.start(mem_conn, "f2", "l2_scene", "v001", None, "lm-studio", "f2")
    RefineRunRecorder.finish(mem_conn, r2, 2, {"create": 2}, "ok")
    # since = 未来时间 → 空
    assert RefineRunRecorder.list_by_stage(mem_conn, "l2_scene", since="2999-01-01T00:00:00Z") == []
    # since = 过去时间 → 全量
    all_runs = RefineRunRecorder.list_by_stage(mem_conn, "l2_scene", since="2020-01-01T00:00:00Z")
    assert len(all_runs) == 2


def test_action_counts_json_roundtrip(mem_conn):
    """action 分布 JSON 存取。"""
    run_id = RefineRunRecorder.start(
        mem_conn, "f1", "l1_conflict", "v001", None, "lm-studio", "f1",
    )
    RefineRunRecorder.finish(
        mem_conn, run_id, memories_count=4,
        action_counts={"store": 3, "skip": 1}, status="ok",
    )
    r = RefineRunRecorder.list_by_stage(mem_conn, "l1_conflict")[0]
    import json as _json
    assert _json.loads(r["action_counts"]) == {"store": 3, "skip": 1}


# ---------- summarize ----------

def _seed_runs(mem_conn):
    """构造 2 版本 × 2 变体 + 记忆行，供 summarize 断言。"""
    for ver, variant, provider, bucket in (
        ("v001", None, "lm-studio", "f1"),
        ("v002", "A", "lm-studio", "f2"),
        ("v002", "B", "deepseek", "f3"),
    ):
        run_id = RefineRunRecorder.start(
            mem_conn, bucket, "l1_extraction", ver, variant, provider, bucket,
        )
        RefineRunRecorder.finish(
            mem_conn, run_id, memories_count=2,
            action_counts={} if variant is None else {"store": 2}, status="ok",
        )
    # 一条 error run（v002/A）
    err = RefineRunRecorder.start(
        mem_conn, "f4", "l1_extraction", "v002", "A", "deepseek", "f4",
    )
    RefineRunRecorder.finish(mem_conn, err, 0, {}, "error", error="解析失败")
    # 记忆行（prompt_version）
    memory_dao.insert_memory(
        mem_conn, content="m1", memory_type="persona", priority=80,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v002",
    )
    memory_dao.insert_memory(
        mem_conn, content="m2", memory_type="persona", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v002",
    )
    memory_dao.insert_memory(
        mem_conn, content="m3", memory_type="persona", priority=90,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v001",
    )


def test_summarize_groups_by_version_variant(mem_conn):
    """summarize 按 (version, variant) 分组。"""
    _seed_runs(mem_conn)
    summary = RefineRunRecorder.summarize(mem_conn, "l1_extraction")
    assert summary["stage"] == "l1_extraction"
    groups = {g["version"] + (g["variant"] or ""): g for g in summary["groups"]}
    # v001/None：1 run，2 memories
    g1 = groups["v001"]
    assert g1["runs"] == 1
    assert g1["error_runs"] == 0
    assert g1["memories_count"] == 2
    # v002/A：2 runs（1 ok + 1 error）
    g2 = groups["v002A"]
    assert g2["runs"] == 2
    assert g2["error_runs"] == 1
    assert g2["memories_count"] == 2
    assert g2["action_dist"] == {"store": 2}
    # v002/B：1 run
    g3 = groups["v002B"]
    assert g3["runs"] == 1
    assert g3["memories_count"] == 2


def test_summarize_avg_priority_from_memories(mem_conn):
    """avg_priority / memories_rows 来自 memories.prompt_version。"""
    _seed_runs(mem_conn)
    summary = RefineRunRecorder.summarize(mem_conn, "l1_extraction")
    by_ver = {}
    for g in summary["groups"]:
        by_ver.setdefault(g["version"], []).append(g)
    # v001：1 条记忆 priority=90
    assert by_ver["v001"][0]["memories_rows"] == 1
    assert by_ver["v001"][0]["avg_priority"] == 90.0
    # v002：2 条记忆 (80, 60) → avg 70；A/B 两行共享该值
    for g in by_ver["v002"]:
        assert g["memories_rows"] == 2
        assert g["avg_priority"] == 70.0


def test_summarize_since_filter(mem_conn):
    """summarize since 过滤 refine_runs（记忆侧不受 since 影响）。"""
    _seed_runs(mem_conn)
    summary = RefineRunRecorder.summarize(mem_conn, "l1_extraction", since="2999-01-01T00:00:00Z")
    assert summary["groups"] == []
    assert summary["since"] == "2999-01-01T00:00:00Z"


def test_summarize_unknown_stage_empty(mem_conn):
    """无记录的 stage → groups 空。"""
    summary = RefineRunRecorder.summarize(mem_conn, "tier0_summary")
    assert summary["groups"] == []


# ---------- insert_memory 向后兼容 ----------

def test_insert_memory_without_prompt_version_backward_compat(mem_conn):
    """旧调用（不传 prompt_version）→ 行为不变，列为 NULL。"""
    mid = memory_dao.insert_memory(
        mem_conn, content="x", memory_type="persona", priority=50,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
    )
    row = mem_conn.execute("SELECT * FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row["prompt_version"] is None


def test_insert_memory_with_prompt_version(mem_conn):
    """新参数 prompt_version 落库。"""
    mid = memory_dao.insert_memory(
        mem_conn, content="x", memory_type="persona", priority=50,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v002",
    )
    row = mem_conn.execute("SELECT * FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row["prompt_version"] == "l1_extraction:v002"


def test_archive_copies_prompt_version(mem_conn):
    """归档时 prompt_version 原样复制到 memory_archive。"""
    mid = memory_dao.insert_memory(
        mem_conn, content="旧", memory_type="persona", priority=80,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v001",
    )
    new_id = memory_dao.insert_memory(
        mem_conn, content="新", memory_type="persona", priority=90,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v002",
    )
    assert memory_dao.archive_memory(mem_conn, mid, superseded_by=new_id)
    arch = memory_dao.find_by_superseded_by(mem_conn, new_id)
    assert len(arch) == 1
    assert arch[0]["prompt_version"] == "l1_extraction:v001"


# ---------- L1.5 P1 F2（T1.7）：finish 候选留痕三列 ----------

def _finish_and_fetch(mem_conn, **finish_kw):
    run_id = RefineRunRecorder.start(
        mem_conn, "f1", "l1_conflict", "v001", None, "lm-studio", "f1",
    )
    RefineRunRecorder.finish(
        mem_conn, run_id, memories_count=0, action_counts={}, status="ok", **finish_kw,
    )
    return mem_conn.execute("SELECT * FROM refine_runs WHERE run_id=?", (run_id,)).fetchone()


def test_finish_candidate_columns_three_states(mem_conn):
    """finish 三列三态：缺省 → NULL；candidate_count 显式 0 与 NULL 区分；有值原样落库。"""
    # 1) 缺省（既有调用形态）：三列均为 NULL
    row1 = _finish_and_fetch(mem_conn)
    assert row1["candidate_count"] is None
    assert row1["candidate_ids_hash"] is None
    assert row1["short_circuit_reason"] is None

    # 2) candidate_count=0 显式传 → 0（与 NULL 区分）
    row2 = _finish_and_fetch(mem_conn, candidate_count=0)
    assert row2["candidate_count"] == 0

    # 3) 批路径形态：candidate_count / candidate_ids_hash 有值，short_circuit_reason 不传 → NULL
    row3 = _finish_and_fetch(
        mem_conn, candidate_count=7, candidate_ids_hash="0123456789abcdef",
    )
    assert row3["candidate_count"] == 7
    assert row3["candidate_ids_hash"] == "0123456789abcdef"
    assert row3["short_circuit_reason"] is None

    # 4) 短路形态：short_circuit_reason 有值（未来 F3 记录形态），候选两列 None
    row4 = _finish_and_fetch(mem_conn, short_circuit_reason="rule_low_sim")
    assert row4["short_circuit_reason"] == "rule_low_sim"
    assert row4["candidate_count"] is None
    assert row4["candidate_ids_hash"] is None


def test_candidate_ids_hash_stable():
    """F2 候选哈希稳定性：同集两次同值、与顺序无关（排序拼接）、异集异值、定长 16。"""
    from sgme.engine import l3_adjudicate as l15_mod

    h1 = l15_mod._candidate_ids_hash(["a", "b"])
    assert h1 == l15_mod._candidate_ids_hash(["a", "b"])   # 同集两次同值
    assert h1 == l15_mod._candidate_ids_hash(["b", "a"])   # 顺序无关
    assert h1 != l15_mod._candidate_ids_hash(["a", "c"])   # 异集异值
    assert len(h1) == 16


# ---------- R1（2026-11-13）：尝试级失败口径汇总 ----------

def _insert_run(conn, run_id, file_id, status, started_at, error=None):
    conn.execute(
        """
        INSERT INTO refine_runs
          (run_id, file_id, stage, version, variant, provider, bucket_key,
           started_at, status, error)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (run_id, file_id, "l1_extraction", "v001", None, "agnes", file_id,
         started_at, status, error),
    )
    conn.commit()


def test_runs_summary_empty(mem_conn):
    """空表：全 0，window_hours 原样回。"""
    from sgme.data import refine_dao

    s = refine_dao.runs_summary(mem_conn)
    assert s == {
        "error_total": 0, "error_window": 0,
        "drop_batch_total": 0, "drop_batch_window": 0,
        "error_files_total": 0, "error_files_window": 0,
        "window_hours": 24,
    }


def test_runs_summary_separates_attempt_and_file(mem_conn):
    """尝试级口径：error 行（含同文件多次）+ drop_batch 子集 + 窗口过滤 + 文件去重。"""
    from datetime import datetime, timedelta, timezone

    from sgme.data import refine_dao

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 同文件 f1：近 24h 两次 drop_batch（模拟 429 风暴下的重试）
    _insert_run(mem_conn, "r1", "f1", "error", recent, "LLM 全链失败: 全链降级失败，drop_batch")
    _insert_run(mem_conn, "r2", "f1", "error", recent, "LLM 全链失败: 全链降级失败，drop_batch")
    # 文件 f2：近 24h 普通解析失败
    _insert_run(mem_conn, "r3", "f2", "error", recent, "输出解析失败")
    # 文件 f3：48h 前 drop_batch（窗口外）
    _insert_run(mem_conn, "r4", "f3", "error", old, "LLM 全链失败: 全链降级失败，drop_batch")
    # 成功行不计数
    _insert_run(mem_conn, "r5", "f4", "ok", recent)

    s = refine_dao.runs_summary(mem_conn)
    assert s["error_total"] == 4          # 尝试级：4 条 error 行
    assert s["error_window"] == 3         # 近 24h：r1/r2/r3
    assert s["drop_batch_total"] == 3     # r1/r2/r4
    assert s["drop_batch_window"] == 2    # r1/r2
    assert s["error_files_total"] == 3    # f1/f2/f3 去重
    assert s["error_files_window"] == 2   # f1/f2
    assert s["window_hours"] == 24

    # 关窗（window_hours=0）→ 窗口字段全 0
    s0 = refine_dao.runs_summary(mem_conn, window_hours=0)
    assert s0["error_window"] == s0["drop_batch_window"] == s0["error_files_window"] == 0
