"""tests/test_append_concurrency.py：append_l0 并发幂等 + raw_files 唯一索引（F-4）。

背景（2026-09-24 深度审查）：append_l0 原为「先查后插」且无唯一约束，
20 线程同参并发 → 11~13 份重复 L0 文件 + 共享连接 InterfaceError。
修复：进程内临界区锁（engine/pipeline._APPEND_L0_LOCK）
+ raw_files (session_key, started_at) 唯一索引（data/db.py 迁移兜底）。

覆盖：
1. 12 线程同 (session_key, started_at) 并发 append → 单条 raw_files 行、零异常、同 file_id
2. 迁移函数：无重复 → 建唯一索引且真发生唯一性约束
3. 迁移函数：存量重复行 → 跳过建索引（不动数据、仅告警）
4. connect_session 新库自动补建索引
"""
from __future__ import annotations

import logging
import sqlite3
import threading

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine import pipeline as pipeline_mod
from sgme.raw import store as raw_store


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录（与 test_operations_append 同口径）。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    for c in (mem_conn, session_conn, wiki_conn):
        try:
            db_mod.close(c)
        except Exception:
            pass


def _make_content(text: str = "并发测试消息") -> str:
    return f"# 2026-09-24T10:00:00Z user\n{text}\n"


# ---------- 1. 并发幂等 ----------


def test_append_l0_concurrent_same_params_single_file(conns, cfg, raw_dir):
    """12 线程同 (session_key, started_at) 并发 → 只落 1 条记录、0 异常、file_id 一致。"""
    mem_conn, session_conn, _ = conns
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(12)

    def _worker():
        try:
            barrier.wait(timeout=5)  # 尽量同时起跑，放大竞态
            r = pipeline_mod.append_l0(
                session_key="race-sess",
                started_at="2026-09-24T10:00:00Z",
                content=_make_content(),
                source_type="session",
                ended_at=None,
                agent_id=None,
                metadata=None,
                cfg=cfg,
                mem_conn=mem_conn,
                session_conn=session_conn,
            )
            results.append(r)
        except Exception as e:  # noqa: BLE001 —— 竞态实验收全异常
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"并发 append 出现异常: {errors[:3]}"
    assert len(results) == 12
    rows = session_conn.execute(
        "SELECT file_id FROM raw_files WHERE session_key='race-sess'"
    ).fetchall()
    assert len(rows) == 1, f"应只有 1 条 raw_files 行，实际 {len(rows)} 条"
    assert len({r["file_id"] for r in results}) == 1, "并发返回的 file_id 应一致"
    # 幂等语义：1 个建文件 + 11 个命中幂等
    assert sum(1 for r in results if r.get("idempotent")) == 11


# ---------- 2/3. 唯一索引迁移 ----------


def _fresh_session_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db_mod.SESSION_DDL)
    return conn


def test_migration_creates_unique_index_and_enforces():
    """无重复行 → 建唯一索引；约束真实生效（同对第二次插入报错）。"""
    conn = _fresh_session_conn()
    try:
        db_mod._migrate_raw_files_session_uniq(conn)

        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_raw_files_session_started" in names

        conn.execute(
            "INSERT INTO raw_files (file_id, path, session_key, started_at) "
            "VALUES ('a', 'raw/a.md', 's1', '2026-01-01T00:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO raw_files (file_id, path, session_key, started_at) "
                "VALUES ('b', 'raw/b.md', 's1', '2026-01-01T00:00:00Z')"
            )
    finally:
        conn.close()


def test_migration_skips_when_duplicates_exist(caplog):
    """存量重复行 → 跳过建索引（不动数据）+ 告警。"""
    conn = _fresh_session_conn()
    try:
        # 竞态产物：不同 file_id、同 (session_key, started_at)
        for fid, name in (("a", "a.md"), ("b", "b.md")):
            conn.execute(
                "INSERT INTO raw_files (file_id, path, session_key, started_at) "
                "VALUES (?, ?, 's1', '2026-01-01T00:00:00Z')",
                (fid, f"raw/{name}"),
            )

        with caplog.at_level(logging.WARNING, logger="sgme.data.db"):
            db_mod._migrate_raw_files_session_uniq(conn)

        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_raw_files_session_started" not in names  # 不建索引
        # 存量数据原样保留（不删不去重）
        cnt = conn.execute("SELECT COUNT(*) FROM raw_files").fetchone()[0]
        assert cnt == 2
        assert any("重复" in (rec.getMessage() or "") for rec in caplog.records)
    finally:
        conn.close()


def test_connect_session_creates_index(tmp_path):
    """新库 connect_session 自动补建唯一索引（幂等迁移链）。"""
    conn = db_mod.connect_session(tmp_path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_raw_files_session_started" in names
    finally:
        conn.close()
