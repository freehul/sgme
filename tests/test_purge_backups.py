# -*- coding: utf-8 -*-
"""tests/test_purge_backups.py：D-44 ③ 备份 purge 工具回归（默认 dry-run + 双钥匙）。"""
from __future__ import annotations

import json

import pytest

from sgme.data import db
from scripts import purge_backups as pb


def _insert(conn, mid, content="内容"):
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, status) VALUES (?,?,?,?,?,?,?,?)",
        (mid, content, "fact", 50, "static", "2026-10-01T00:00:00Z",
         "2026-10-01T00:00:00Z", "active"),
    )
    conn.execute(
        "INSERT INTO memory_archive (memory_id, content, memory_type, priority, "
        "time_velocity, created_at, updated_at, archived_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (mid, content, "fact", 50, "static", "2026-10-01T00:00:00Z",
         "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"),
    )


@pytest.fixture()
def env(tmp_path):
    live = tmp_path / "data"
    mem = db.connect_memory(live)
    _insert(mem, "m1")
    _insert(mem, "m2")
    mem.execute(
        "INSERT INTO deletion_audit (memory_id, mode, triggered_by, reason, created_at) "
        "VALUES ('m1','purge','user','用户要求','2026-10-03T00:00:00Z')"
    )
    mem.commit()
    mem.close()
    backups = tmp_path / "backups"
    snap = backups / "full_20261001_030000_aaaaaaaa"
    snap.mkdir(parents=True)
    smem = db.connect_memory(snap)
    _insert(smem, "m1")
    _insert(smem, "m2")
    smem.commit()
    smem.close()
    return live, backups, snap


def test_purged_ids_excludes_sentinel(env):
    live, _b, _s = env
    assert pb.purged_ids(live) == ["m1"]


def test_dry_run_does_not_touch_snapshot(env):
    live, backups, snap = env
    rep = pb.run(live, backups, apply=False)
    assert rep["purged_ids"] == 1 and rep["total_matched"] >= 1
    assert rep["snapshots"][0]["applied"] is False
    conn = db.connect_memory(snap)
    n = conn.execute("SELECT COUNT(*) c FROM memories WHERE memory_id='m1'").fetchone()["c"]
    conn.close()
    assert n == 1


def test_apply_removes_deleted_content_keeps_others(env):
    live, backups, snap = env
    rep = pb.run(live, backups, apply=True)
    assert rep["snapshots"][0]["applied"] is True
    conn = db.connect_memory(snap)
    ids = [r["memory_id"] for r in conn.execute("SELECT memory_id FROM memories").fetchall()]
    arch = [r["memory_id"] for r in conn.execute(
        "SELECT memory_id FROM memory_archive").fetchall()]
    conn.close()
    assert ids == ["m2"] and arch == ["m2"]


def test_main_requires_confirm(env, tmp_path):
    live, backups, _snap = env
    out = tmp_path / "report.json"
    rc = pb.main([
        "--data-dir", str(live), "--backup-dir", str(backups),
        "--apply", "--out", str(out),
    ])
    assert rc == 2
    assert not out.exists()


def test_main_apply_writes_report_and_audit(env, tmp_path):
    live, backups, snap = env
    out = tmp_path / "report.json"
    rc = pb.main([
        "--data-dir", str(live), "--backup-dir", str(backups),
        "--apply", "--confirm", pb.CONFIRM_PHRASE,
        "--out", str(out), "--json",
    ])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["apply"] is True and report["total_matched"] >= 1
    conn = db.connect_memory(live)
    row = conn.execute(
        "SELECT reason FROM deletion_audit WHERE memory_id=?", (pb.SENTINEL,)
    ).fetchone()
    conn.close()
    assert row is not None and row["reason"].startswith("backup_purge:")
