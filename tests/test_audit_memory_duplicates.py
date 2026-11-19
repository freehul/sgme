"""tests: 记忆语义重复审计脚本（scripts/audit_memory_duplicates.py）。

覆盖：向量解码 / 余弦阈值 / i<j 去重 / 并查集聚簇 / 按版本分组 / 缺向量计数 / 只读不写。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_memory_duplicates.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_mem_dup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def db(tmp_path):
    """造一个最小库：3 条 v023（其中 2 条同义）+ 2 条 v025（1 对同义）+ 1 条无向量。"""
    p = tmp_path / "memory.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE memories (
          memory_id TEXT PRIMARY KEY, content TEXT, prompt_version TEXT,
          created_at TEXT, route TEXT, status TEXT DEFAULT 'active');
        CREATE TABLE memory_vectors (
          memory_id TEXT PRIMARY KEY, embedding BLOB NOT NULL,
          model TEXT NOT NULL, dims INTEGER NOT NULL, embedded_at TEXT NOT NULL);
        """
    )
    base = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    near = np.array([0.98, 0.15, 0.05], dtype=np.float32)
    other = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    # v025 组用正交方向的另一簇，避免与 v023 组互相误判（测试数据要能把"组内重复"和"跨组"分开）
    v25a = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    v25b = np.array([0.05, 0.05, 0.9975], dtype=np.float32)

    rows = [
        ("m1", "按钮不区分 IDE 与 agent 布局", "l1_extraction:v023", "2026-11-19T01:00:00Z", "memory", base),
        ("m2", "顶栏按钮不区分 IDE/agent 布局，统一框架", "l1_extraction:v023", "2026-11-19T01:00:01Z", "memory", near),
        ("m3", "另一个完全不同的结论", "l1_extraction:v023", "2026-11-19T01:00:02Z", "memory", other),
        ("m4", "部署后 active 切到 v025", "l1_extraction:v025", "2026-11-19T09:00:00Z", "memory", v25a),
        ("m5", "部署后 active 切换到 v025 版本", "l1_extraction:v025", "2026-11-19T09:00:01Z", "memory", v25b),
    ]
    for mid, content, ver, ts, route, vec in rows:
        conn.execute("INSERT INTO memories VALUES (?,?,?,?,?,?)", (mid, content, ver, ts, route, "active"))
        conn.execute(
            "INSERT INTO memory_vectors VALUES (?,?,?,?,?)",
            (mid, vec.tobytes(), "bge-m3", 3, ts),
        )
    # 无向量的记忆
    conn.execute(
        "INSERT INTO memories VALUES (?,?,?,?,?,?)",
        ("m6", "没有向量的记忆", "l1_extraction:v025", "2026-11-19T09:00:02Z", "memory", "active"),
    )
    # 已 rejected：不应计入
    conn.execute(
        "INSERT INTO memories VALUES (?,?,?,?,?,?)",
        ("m7", "被拒的记忆", "l1_extraction:v023", "2026-11-19T01:00:03Z", "memory", "rejected"),
    )
    conn.commit()
    conn.close()
    return p


def test_finds_near_duplicates_and_clusters(db):
    mod = _load()
    conn = mod.connect_ro(db)
    rows, missing = mod.load_rows(conn, "2026-11-19", None)
    conn.close()
    assert len(rows) == 5, "应取到 5 条有向量的非 rejected 记忆"
    assert missing == 1, "m6 无向量要单独计入缺向量"

    keep, mat = mod.to_matrix(rows)
    pairs = mod.find_pairs(mat, 0.92)
    # 两对：m1-m2、m4-m5
    assert len(pairs) == 2
    assert all(i < j for i, j, _ in pairs)

    clusters = mod.cluster(pairs, len(keep))
    assert sorted(len(c) for c in clusters) == [2, 2]


def test_version_breakdown(db):
    mod = _load()
    conn = mod.connect_ro(db)
    rows, missing = mod.load_rows(conn, "2026-11-19", ["l1_extraction:v025"])
    conn.close()
    assert [r["memory_id"] for r in rows] == ["m4", "m5"]
    keep, mat = mod.to_matrix(rows)
    pairs = mod.find_pairs(mat, 0.92)
    report = mod.build_report(keep, pairs, mod.cluster(pairs, len(keep)), 0.92, len(rows) + missing, missing)
    assert report["by_version"]["l1_extraction:v025"]["pairs"] == 1


def test_threshold_respected(db):
    mod = _load()
    conn = mod.connect_ro(db)
    rows, _ = mod.load_rows(conn, None, None)
    conn.close()
    keep, mat = mod.to_matrix(rows)
    strict = mod.find_pairs(mat, 0.999)
    loose = mod.find_pairs(mat, 0.5)
    assert len(strict) <= len(loose)


def test_readonly_and_json_cli(db, capsys):
    mod = _load()
    before = db.stat().st_mtime_ns
    assert mod.main(["--db", str(db), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows_with_vector"] == 5
    assert out["dup_pairs"] == 2
    assert db.stat().st_mtime_ns == before, "审计必须只读，库文件不许被改写"


def test_missing_db_returns_2(tmp_path, capsys):
    mod = _load()
    assert mod.main(["--db", str(tmp_path / "nope.db")]) == 2
