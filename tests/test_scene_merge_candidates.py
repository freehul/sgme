# -*- coding: utf-8 -*-
"""tests/test_scene_merge_candidates.py：9.4 #10 只读合并候选回归。"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

from scripts.scene_merge_candidates import candidates


def _vec(vals):
    return struct.pack("<" + "f" * len(vals), *vals)


def _make_db(root: Path) -> Path:
    data = root / "data"
    data.mkdir()
    db = data / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE scenes (scene_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "content TEXT NOT NULL DEFAULT '', heat INTEGER DEFAULT 1, "
            "status TEXT NOT NULL DEFAULT 'active', created_at TEXT DEFAULT '', "
            "updated_at TEXT DEFAULT '')"
        )
        conn.execute(
            "CREATE TABLE scene_vectors (scene_id TEXT PRIMARY KEY, embedding BLOB NOT NULL, "
            "model TEXT NOT NULL, dims INTEGER NOT NULL, embedded_at TEXT NOT NULL)"
        )
        rows = [
            ("s1", "工作项目复盘", "active", _vec([1, 0, 0, 0])),
            ("s2", "工作项目复盘（二）", "active", _vec([0.99, 0.01, 0, 0])),
            ("s3", "旅行计划", "active", _vec([0, 1, 0, 0])),
            ("s4", "工作项目复盘", "active", _vec([0, 0, 1, 0])),
            ("s5", "工作项目复盘", "archived", _vec([1, 0, 0, 0])),
        ]
        for sid, title, status, blob in rows:
            conn.execute(
                "INSERT INTO scenes (scene_id, title, status) VALUES (?,?,?)",
                (sid, title, status),
            )
            conn.execute(
                "INSERT INTO scene_vectors VALUES (?,?,?,?,?)",
                (sid, blob, "test", 4, "2026-11-04T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()
    return data


def test_finds_near_duplicate_scene(tmp_path):
    data = _make_db(tmp_path)
    out = candidates(str(data))
    assert out["available"] is True
    assert out["scenes"] == 4  # active only，s5 archived 不计
    pairs = {(c["a"], c["b"]) for c in out["candidates"]}
    assert ("s1", "s2") in pairs
    # s4 标题相同但向量正交；s3 标题不同
    assert ("s1", "s4") not in pairs
    assert ("s1", "s3") not in pairs


def test_read_only_bytes_unchanged(tmp_path):
    data = _make_db(tmp_path)
    db = data / "memory.db"
    before = db.read_bytes()
    candidates(str(data))
    assert db.read_bytes() == before


def test_missing_db_degrades(tmp_path):
    out = candidates(str(tmp_path / "none"))
    assert out["available"] is False
    assert out["candidates"] == []
    assert out["warnings"]


def test_threshold_and_top(tmp_path):
    data = _make_db(tmp_path)
    strict = candidates(str(data), threshold=0.999999)
    assert strict["candidates"] == []
    limited = candidates(str(data), top=1)
    assert len(limited["candidates"]) <= 1
