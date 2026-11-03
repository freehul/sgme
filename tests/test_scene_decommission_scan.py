# -*- coding: utf-8 -*-
"""tests/test_scene_decommission_scan.py：9.6 只读扫描回归（不删库、不改代码）。"""
from __future__ import annotations

from sgme import scene_scan
from sgme.data import db


def test_scan_code_finds_scene_consumers():
    rep = scene_scan.scan_code()
    assert rep["files_scanned"] > 0
    assert rep["endpoint_hits"] > 0, "仓库内应能扫到场景端点字面量"
    paths = [f["path"] for f in rep["top_files"]]
    assert any(p.startswith("sgme/") for p in paths)
    assert all(isinstance(p, str) for p in rep["consumer_files"])


def test_scan_db_counts_tables(tmp_path):
    conn = db.connect_memory(tmp_path)
    conn.execute(
        "INSERT INTO scenes (scene_id, title, content, created_at, updated_at) "
        "VALUES ('s1','场景一','内容','2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO scene_vectors (scene_id, embedding, model, dims, embedded_at) "
        "VALUES ('s1', X'00', 'bge-m3', 1024, '2026-10-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    rep = scene_scan.scan_db(tmp_path)
    assert rep["available"] is True
    assert rep["tables"]["scenes"]["rows"] == 1
    assert rep["tables"]["scene_vectors"]["rows"] == 1


def test_scan_db_missing_returns_unavailable(tmp_path):
    rep = scene_scan.scan_db(tmp_path / "nope")
    assert rep["available"] is False


def test_build_report_has_checklist():
    rep = scene_scan.build_report()
    assert rep["checklist"] and all("action" in c for c in rep["checklist"])
    assert "未改库" in rep["note"]
