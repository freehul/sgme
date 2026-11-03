# -*- coding: utf-8 -*-
"""tests/test_verify_migration.py：§9.4 十项迁移校验的判定口径回归。

思路：先用阶段 0 采集器对一份临时库出基线，再跑校验器——
- 未改动 → 机器可判项应全 PASS（知识库修订史缺页按设计降为 WARN）；
- 改正文 → #2 FAIL；删记忆 → #1 FAIL + #2 missing；少向量 → #3 FAIL；
- 页数变化 → #6 FAIL。防止门禁写成"永远绿"。
"""
from __future__ import annotations

import json

from sgme.data import credentials_dao, db
from scripts import migrate_2_0_baseline as mb
from scripts import verify_migration as vm


def _build(path):
    path.mkdir(parents=True, exist_ok=True)
    (path.parent / "raw").mkdir(exist_ok=True)
    mem = db.connect_memory(path)
    mem.execute(
        "INSERT INTO dimension_registry (id, display_name, category, time_velocity, created_at) "
        "VALUES ('identity','身份','静态','static','2026-10-01T00:00:00Z')"
    )
    for i in range(3):
        mem.execute(
            "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
            "created_at, updated_at, status, scene_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"m{i}", f"content-{i}", "fact", 50, "static",
             "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", "active", "identity:fact"),
        )
        mem.execute(
            "INSERT INTO memory_tags (memory_id, dimension_id) VALUES (?, 'identity')", (f"m{i}",)
        )
        mem.execute(
            "INSERT INTO memory_vectors (memory_id, embedding, model, dims, embedded_at) "
            "VALUES (?, X'00', 'mock', 1, '2026-10-01T00:00:00Z')", (f"m{i}",)
        )
    mem.execute("CREATE TABLE memories_fts(x)")
    mem.executemany("INSERT INTO memories_fts(x) VALUES (?)", [("a",), ("b",), ("c",)])
    mem.execute(
        "INSERT INTO memory_archive (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, archived_at) VALUES ('a1','old','fact',50,'static',"
        "'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','2026-09-02T00:00:00Z')"
    )
    mem.commit()
    mem.close()

    ses = db.connect_session(path)
    ses.commit()
    ses.close()

    wiki = db.connect_wiki(path)
    wiki.execute("INSERT INTO wiki_pages (page_id,title,content,status) VALUES ('p1','t','c','active')")
    wiki.commit()
    wiki.close()

    sk = db.connect_skills(path)
    sk.execute("INSERT INTO skills (name, sha256, content) VALUES ('alpha','h','body')")
    sk.commit()
    sk.close()

    cr = credentials_dao.connect_credentials(path)
    cr.commit()
    cr.close()


def _baseline(tmp_path):
    data = tmp_path / "data"
    _build(data)
    out = tmp_path / "migration"
    mb.run(data, out, label="20261104")
    return data, out / "2.0-迁移基线-20261104.json"


def _checks(report) -> dict:
    return {c["no"]: c for c in report["checks"]}


def test_unchanged_db_passes_machine_checks(tmp_path):
    data, baseline = _baseline(tmp_path)
    report = vm.run(baseline, data, out_dir=tmp_path / "verify")
    checks = _checks(report)
    assert report["summary"]["fail"] == 0, report["checks"]
    assert checks[1]["status"] == vm.PASS
    assert checks[2]["status"] == vm.PASS
    assert checks[3]["status"] == vm.PASS
    assert checks[4]["status"] == vm.PASS
    assert checks[5]["status"] == vm.PASS
    assert checks[7]["status"] == vm.PASS
    assert checks[8]["status"] == vm.PASS
    # 知识库：九列已建、修订史为空 → 修订史缺页 WARN（阶段 3 门禁）
    assert checks[6]["status"] == vm.WARN
    assert checks[9]["status"] == vm.MANUAL and checks[10]["status"] == vm.MANUAL
    assert (tmp_path / "verify").glob("2.0-迁移校验-*.md")


def test_content_change_fails_integrity(tmp_path):
    data, baseline = _baseline(tmp_path)
    conn = db.connect_memory(data)
    conn.execute("UPDATE memories SET content='changed' WHERE memory_id='m0'")
    conn.commit()
    conn.close()
    report = vm.run(baseline, data, out_dir=tmp_path / "verify")
    checks = _checks(report)
    assert checks[2]["status"] == vm.FAIL
    assert report["summary"]["fail"] >= 1


def test_row_count_change_fails_conservation(tmp_path):
    data, baseline = _baseline(tmp_path)
    conn = db.connect_memory(data)
    # 外键：先清子表再删主表（FK 是 ON，不能直接删 memories）
    conn.execute("DELETE FROM memory_vectors WHERE memory_id='m2'")
    conn.execute("DELETE FROM memory_tags WHERE memory_id='m2'")
    conn.execute("DELETE FROM memories WHERE memory_id='m2'")
    conn.commit()
    conn.close()
    report = vm.run(baseline, data, out_dir=tmp_path / "verify")
    checks = _checks(report)
    assert checks[1]["status"] == vm.FAIL
    assert checks[2]["status"] == vm.FAIL, "抽样 id 缺失也算完整性失败"


def test_vector_regression_fails(tmp_path):
    data, baseline = _baseline(tmp_path)
    conn = db.connect_memory(data)
    conn.execute("DELETE FROM memory_vectors WHERE memory_id='m1'")
    conn.commit()
    conn.close()
    report = vm.run(baseline, data, out_dir=tmp_path / "verify")
    assert _checks(report)[3]["status"] == vm.FAIL


def test_wiki_count_change_fails(tmp_path):
    data, baseline = _baseline(tmp_path)
    conn = db.connect_wiki(data)
    conn.execute("INSERT INTO wiki_pages (page_id,title,content,status) VALUES ('p2','t','c','active')")
    conn.commit()
    conn.close()
    report = vm.run(baseline, data, out_dir=tmp_path / "verify")
    assert _checks(report)[6]["status"] == vm.FAIL


def test_scene_key_missing_fails(tmp_path):
    data, baseline = _baseline(tmp_path)
    conn = db.connect_memory(data)
    conn.execute("UPDATE memories SET scene_key=NULL")
    conn.commit()
    conn.close()
    report = vm.run(baseline, data, out_dir=tmp_path / "verify", export_unknown=True)
    assert _checks(report)[8]["status"] == vm.FAIL
    assert report["unknown_scene_key"], "未回填清单必须可导出"


def test_main_returns_nonzero_on_fail(tmp_path):
    data, baseline = _baseline(tmp_path)
    conn = db.connect_memory(data)
    conn.execute("UPDATE memories SET content='changed' WHERE memory_id='m0'")
    conn.commit()
    conn.close()
    rc = vm.main(["--baseline", str(baseline), "--data-dir", str(data),
                  "--out-dir", str(tmp_path / "verify")])
    assert rc == 1
