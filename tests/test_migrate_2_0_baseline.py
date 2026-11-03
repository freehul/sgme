# -*- coding: utf-8 -*-
"""tests/test_migrate_2_0_baseline.py：迁移阶段 0 采集器（scripts/migrate_2_0_baseline.py）。

判定口径：
- 报告结构：JSON + Markdown 双落盘，§9.1 关键计数与库内真实值一致；
- 抽样：memories / wiki / skills 哈希清单按 sample 上限截断且确定性（按 id 排序）；
- 只读：采集前后 *.db 文件 sha256 必须一字不差（采集器绝不写库）；
- 缺库：memory.db 不存在时 run() 抛 FileNotFoundError、main() 返回 1（不静默产出空基线）。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from sgme.data import credentials_dao, db
from scripts import migrate_2_0_baseline as mb


def _digests(data_dir):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(data_dir.glob("*.db"))
    }


def _build_data_dir(path):
    # 生产布局：SGME_HOME/raw 与 SGME_HOME/data 是兄弟目录（容器里 /data/raw + /data/data）
    path.mkdir(parents=True, exist_ok=True)
    raw_dir = path.parent / "raw"
    raw_dir.mkdir(exist_ok=True)
    (raw_dir / "s1.md").write_text("# 2026-10-01T00:00:00Z user\nhi\n", encoding="utf-8")
    mem = db.connect_memory(path)
    for i in range(3):
        mem.execute(
            "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
            "created_at, updated_at, status) VALUES (?,?,?,?,?,?,?,?)",
            (f"m{i}", f"content-{i}", "fact", 50, "static",
             "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", "active"),
        )
    mem.execute(
        "INSERT INTO memory_archive (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, archived_at) VALUES ('a1','old','fact',50,'static',"
        "'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','2026-09-02T00:00:00Z')"
    )
    mem.execute(
        "INSERT INTO memory_vectors (memory_id, embedding, model, dims, embedded_at) "
        "VALUES ('m0', X'00', 'mock', 1, '2026-10-01T00:00:00Z')"
    )
    mem.execute(
        "INSERT INTO dimension_registry (id, display_name, category, time_velocity, created_at) "
        "VALUES ('identity','身份','静态','static','2026-10-01T00:00:00Z')"
    )
    mem.execute("INSERT INTO memory_tags (memory_id, dimension_id) VALUES ('m0','identity')")
    mem.commit()
    mem.close()

    ses = db.connect_session(path)
    ses.execute(
        "INSERT INTO raw_files (file_id, path, session_key, status) VALUES ('f1','p','s','refined')"
    )
    ses.commit()
    ses.close()

    wiki = db.connect_wiki(path)
    wiki.execute("INSERT INTO wiki_pages (page_id, title, content, status) VALUES ('p1','t','c','active')")
    wiki.commit()
    wiki.close()

    sk = db.connect_skills(path)
    sk.execute("INSERT INTO skills (name, sha256, content) VALUES ('alpha','h','body')")
    sk.commit()
    sk.close()

    cr = credentials_dao.connect_credentials(path)
    cr.execute(
        "INSERT INTO credentials (key_name, value_cipher, created_at, updated_at, updated_by) "
        "VALUES ('k1','cipher','t','t','test')"
    )
    cr.commit()
    cr.close()


def test_baseline_report_shape_and_counts(tmp_path):
    data = tmp_path / "data"
    out = tmp_path / "out"
    _build_data_dir(data)
    before = _digests(data)

    report = mb.run(data, out, label="20261104")

    json_path = out / "2.0-迁移基线-20261104.json"
    md_path = out / "2.0-迁移基线-20261104.md"
    assert json_path.is_file() and md_path.is_file()
    on_disk = json.loads(json_path.read_text(encoding="utf-8"))
    assert on_disk["kind"] == "sgme-2.0-migration-baseline"
    assert on_disk["label"] == "20261104"

    mem = report["databases"]["memory"]
    assert mem["memories"]["total"] == 3
    assert mem["memories"]["by_status"] == {"active": 3}
    assert mem["memory_archive"] == 1
    assert mem["memory_vectors"]["rows"] == 1
    assert mem["memory_vectors"]["missing"] == 2
    assert mem["memory_tags"] == 1
    assert mem["agent_tag_coverage"]["tagged"] == 0
    two = mem["two_0"]
    assert two["columns_present"] == two["columns_total"], "阶段 1 后 2.0 列应全在"
    assert all(two["tables"].values()), "阶段 1 后 2.0 正式表应全在"
    # 新库：DDL 带列但写入侧尚未判定 → NULL（注入谓词兜底视为 active，§3.6）
    assert two["lifecycle_distribution"] == {"<null>": 3}

    assert report["databases"]["session"]["raw_files"] == 1
    assert report["databases"]["session"]["raw_files_by_agent"] == {"<null>": 1}
    assert report["databases"]["session"]["schema_versions"], "session.db 应报告 schema_versions"
    assert report["databases"]["session"]["raw_dir"]["files"] == 1, "应命中兄弟目录 raw/"
    assert report["databases"]["wiki"]["schema_versions"]
    assert report["databases"]["wiki"]["wiki_pages"] == 1
    assert report["databases"]["wiki"]["two_0"]["columns_present"] == 9
    assert report["databases"]["skills"]["skills"] == 1
    assert report["databases"]["credentials"]["credentials"] == 1
    assert report["databases"]["credentials"]["two_0"]["columns_present"] == 4

    assert len(report["hashes"]["memories"]) == 3
    assert len(report["hashes"]["wiki"]) == 1
    assert len(report["hashes"]["skills"]) == 1
    assert report["hashes"]["memories"][0]["id"] == "m0", "抽样必须确定性（按 id 升序）"

    md = md_path.read_text(encoding="utf-8")
    assert "迁移基线" in md
    assert "§9.1 基线表" in md
    assert "memories" in md

    assert _digests(data) == before, "采集器不得写库（只读）"


def test_baseline_sample_limit(tmp_path):
    data = tmp_path / "data"
    _build_data_dir(data)
    report = mb.run(data, tmp_path / "out", sample_memories=1, sample_wiki=0, sample_skills=0,
                    label="small")
    assert len(report["hashes"]["memories"]) == 1
    assert report["hashes"]["wiki"] == []
    assert report["hashes"]["skills"] == []


def test_baseline_missing_memory_db(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        mb.run(empty, tmp_path / "out")
    assert mb.main(["--data-dir", str(empty), "--out-dir", str(tmp_path / "out")]) == 1


def test_baseline_tolerates_missing_optional_dbs(tmp_path):
    """只有 memory.db 时也要出报告：其余库记 null + warnings（生产可能禁用了扩展模块）。"""
    data = tmp_path / "data"
    data.mkdir()
    mem = db.connect_memory(data)
    mem.commit()
    mem.close()
    report = mb.run(data, tmp_path / "out", label="minimal")
    assert report["databases"]["memory"]["memories"]["total"] == 0
    assert report["databases"]["session"] is None
    assert any("session.db" in w for w in report["warnings"])
