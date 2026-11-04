# -*- coding: utf-8 -*-
"""tests/test_ops_health.py：运维健康总览（存储体检 + 9.6 就绪，B300）。"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from sgme.operations.ops_health import health_overview


def _make_data(root: Path) -> tuple[Path, Path]:
    data = root / "data"
    data.mkdir(parents=True)
    conn = sqlite3.connect(data / "memory.db")
    try:
        conn.execute("CREATE TABLE scenes (id TEXT)")
        conn.execute("CREATE TABLE scene_vectors (id TEXT)")
        conn.execute("CREATE TABLE scenes_fts (id TEXT)")
        conn.executemany("INSERT INTO scenes VALUES (?)", [("s1",), ("s2",), ("s3",)])
        conn.executemany("INSERT INTO scene_vectors VALUES (?)", [("v1",), ("v2",)])
        conn.execute("INSERT INTO scenes_fts VALUES ('f1')")
        conn.commit()
    finally:
        conn.close()
    (data / "wiki.db").write_bytes(b"x" * 128)
    raw = data / "raw"
    raw.mkdir()
    (raw / "a.md").write_bytes(b"a" * 64)
    (raw / "b.md").write_bytes(b"b" * 32)
    return data, raw


def _cfg(data: Path, raw: Path, backups: Path, *, stable_since: str | None = None) -> dict:
    cfg = {
        "paths": {"data_dir": str(data), "raw_dir": str(raw)},
        "backup": {"dir": str(backups)},
        "compat": {"stable_since": stable_since, "enforce": False, "enforce_gone": False},
    }
    return cfg


def test_storage_and_scene_counts(tmp_path):
    data, raw = _make_data(tmp_path)
    backups = tmp_path / "backups" / "full_1"
    backups.mkdir(parents=True)
    (backups / "memory.db").write_bytes(b"z" * 256)
    out = health_overview(_cfg(data, raw, tmp_path / "backups"))

    st = out["storage"]
    assert st["memory_db_bytes"] > 0 and st["wiki_db_bytes"] == 128
    assert st["raw_files"] == 2 and st["raw_bytes"] == 96
    assert st["backup_snapshots"] == 1 and st["backup_bytes"] >= 256

    sc = out["scenes"]
    assert (sc["scenes"], sc["scene_vectors"], sc["scenes_fts"]) == (3, 2, 1)
    assert sc["gone_rules"] > 0
    assert sc["compat_phase"] == "pre_stable"
    assert sc["ready"] is False
    assert any("stable_since" in b for b in sc["blockers"])


def test_ready_when_compat_active_and_rules_present(tmp_path):
    data, raw = _make_data(tmp_path)
    since = (date.today() - timedelta(days=5)).isoformat()
    out = health_overview(_cfg(data, raw, tmp_path / "backups", stable_since=since))
    sc = out["scenes"]
    assert sc["compat_phase"] == "active"
    assert sc["ready"] is True
    assert sc["blockers"] == []
    assert out["manual_actions"]


def test_invalid_stable_since_blocks(tmp_path):
    data, raw = _make_data(tmp_path)
    out = health_overview(_cfg(data, raw, tmp_path / "backups", stable_since="not-a-date"))
    sc = out["scenes"]
    assert sc["compat_phase"] == "unknown"
    assert sc["ready"] is False
    assert any("非法" in b for b in sc["blockers"])


def test_missing_database_degrades(tmp_path):
    data = tmp_path / "empty"
    data.mkdir()
    out = health_overview({
        "paths": {"data_dir": str(data), "raw_dir": str(data / "raw")},
        "backup": {"dir": str(tmp_path / "nobackup")},
    })
    assert out["storage"]["memory_db_bytes"] == 0
    assert out["storage"]["raw_files"] == 0
    assert out["scenes"]["scenes"] == 0
    assert out["scenes"]["ready"] is False
    assert any("memory.db 不可用" in b for b in out["scenes"]["blockers"])
