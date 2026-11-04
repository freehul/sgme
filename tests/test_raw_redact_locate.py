# -*- coding: utf-8 -*-
"""tests/test_raw_redact_locate.py：D-44 raw 段落定位（只读）回归。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.raw_redact_locate import locate


def _make_data(root: Path) -> Path:
    data = root / "data"
    raw = data / "raw"
    raw.mkdir(parents=True)
    (raw / "session_a.md").write_text(
        "# 2026-11-01 user\n今天聊了跑步计划。\n\n"
        "# 2026-11-01 assistant\n用户对花生过敏，不能吃花生酱。\n\n"
        "# 2026-11-01 user\n周末去看电影。\n",
        encoding="utf-8",
    )
    (raw / "session_b.md").write_text(
        "# 2026-11-02 user\n秘密项目X 的进度不要外传。\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(data / "memory.db")
    try:
        conn.execute(
            "CREATE TABLE deletion_audit (memory_id TEXT, mode TEXT, "
            "snapshot_json TEXT, created_at TEXT)"
        )
        snap = {"memory": {"content": "用户对花生过敏，不能吃花生酱"}, "sources": []}
        conn.execute(
            "INSERT INTO deletion_audit VALUES (?,?,?,?)",
            ("mem-1", "purge", json.dumps(snap, ensure_ascii=False), "2026-11-04T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    return data


def test_locate_by_memory_snapshot(tmp_path):
    data = _make_data(tmp_path)
    out = locate(["mem-1"], [], str(data), threshold=0.5)
    assert out["read_only"] is True
    hit = [m for m in out["matches"] if m["memory_id"] == "mem-1"]
    assert hit, out
    assert hit[0]["file"].endswith("session_a.md")
    assert hit[0]["score"] >= 0.5
    assert "花生过敏" in hit[0]["preview"]


def test_locate_by_keyword(tmp_path):
    data = _make_data(tmp_path)
    out = locate([], ["秘密项目X"], str(data))
    assert out["matches"]
    m = out["matches"][0]
    assert m["keyword"] == "秘密项目X"
    assert m["file"].endswith("session_b.md")


def test_read_only_bytes_unchanged(tmp_path):
    data = _make_data(tmp_path)
    raw = data / "raw" / "session_a.md"
    before = raw.read_bytes()
    locate(["mem-1"], [], str(data), threshold=0.5)
    assert raw.read_bytes() == before


def test_missing_raw_dir_degrades(tmp_path):
    out = locate([], ["x"], str(tmp_path / "none"))
    assert out["files_scanned"] == 0
    assert out["matches"] == []
    assert out["warnings"]
