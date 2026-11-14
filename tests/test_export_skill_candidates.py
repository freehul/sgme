# -*- coding: utf-8 -*-
"""tests/test_export_skill_candidates.py：T5a 技能候选导出分片脚本（2026-11-15）。

验收（任务清单-技能自动命名-20261115.md T5a）：
1. 分片数量正确、每片 <= part_size；
2. 同 content_sha256 只出现在一个分片里（一份正文一个名字）；
3. 已有分片文件不被覆盖（断点续跑）；
4. suggested_name IS NOT NULL 的候选不被导出；
5. 空候选池 → 不产生文件、退出码 0；
6. 老库（无 suggested_name 列）也能导出（只读连接不能迁移，必须容忍）；
7. --only-unexported：跳过已出现在既有分片里的候选，编号接续。

全部离线：临时 sqlite，不触碰生产库。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_script(module_name: str, file_name: str):
    """按路径加载 scripts/ 下脚本为模块（脚本非包成员，直接 import 不可靠）。"""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / file_name)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


exp = _load_script("export_skill_candidates", "export_skill_candidates.py")


def _add_suggested_name_col(conn: sqlite3.Connection) -> None:
    """给测试库补 suggested_name 列（T5c 落地后 connect_skills 自带，此处幂等兜底）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(skill_candidates)").fetchall()}
    if "suggested_name" not in cols:
        conn.execute("ALTER TABLE skill_candidates ADD COLUMN suggested_name TEXT")
        conn.commit()


@pytest.fixture
def skills_db(tmp_path):
    """带候选池的 skills.db（新 schema：含 suggested_name 列）。"""
    from sgme.data import db as db_mod

    conn = db_mod.connect_skills(tmp_path)
    _add_suggested_name_col(conn)
    yield conn
    conn.close()


def _insert_candidate(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    sha: str | None = None,
    description: str = "测试候选描述",
    content: str = "---\nname: exp-test\n---\n\n正文",
    status: str = "accepted",
    suggested_name: str | None = None,
) -> None:
    now = "2026-11-15T00:00:00Z"
    conn.execute(
        "INSERT INTO skill_candidates (memory_id, name, description, content, category,"
        " tags, confidence, status, source, content_sha256, suggested_name,"
        " created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (memory_id, f"exp-{memory_id[:8]}", description, content, "testing",
         "[]", 0.95, status, "memory_route", sha, suggested_name, now, now),
    )
    conn.commit()


def _read_parts(out_dir: Path) -> dict[str, dict]:
    return {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(out_dir.glob("part-*.json"))
        if not p.name.endswith(".names.json")
    }


def test_slices_and_item_contract(skills_db, tmp_path):
    for i in range(45):
        _insert_candidate(skills_db, f"m-{i:03d}", sha=f"sha-{i:03d}")
    out = tmp_path / "naming"
    rc = exp.main(["--out", str(out), "--part-size", "20", "--db", str(tmp_path / "skills.db")])
    assert rc == 0
    parts = _read_parts(out)
    assert sorted(parts) == ["part-001.json", "part-002.json", "part-003.json"]
    sizes = [len(p["items"]) for p in parts.values()]
    assert sizes == [20, 20, 5]
    for name, part in parts.items():
        num = int(name[5:8])
        assert part["part"] == num and part["part_size"] == 20
        assert part["total_parts"] == 3
        for idx, item in enumerate(part["items"]):
            assert item["i"] == idx
            assert set(item) == {"i", "memory_id", "sha", "description", "content_head"}
            assert len(item["content_head"]) <= 300
    # 全量覆盖且顺序稳定（candidate_id 序）
    all_ids = [it["memory_id"] for p in parts.values() for it in p["items"]]
    assert all_ids == [f"m-{i:03d}" for i in range(45)]


def test_same_sha_exported_once(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-first", sha="sha-dup")
    _insert_candidate(skills_db, "m-second", sha="sha-dup")
    _insert_candidate(skills_db, "m-third", sha="sha-unique")
    out = tmp_path / "naming"
    rc = exp.main(["--out", str(out), "--part-size", "20", "--db", str(tmp_path / "skills.db")])
    assert rc == 0
    parts = _read_parts(out)
    ids = [it["memory_id"] for p in parts.values() for it in p["items"]]
    assert ids == ["m-first", "m-third"]  # 同指纹只出一次，代表取 candidate_id 最小


def test_existing_part_files_not_overwritten(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-a", sha="sha-a")
    out = tmp_path / "naming"
    out.mkdir(parents=True)
    sentinel = {"part": 1, "total_parts": 1, "part_size": 20, "items": [], "sentinel": True}
    (out / "part-001.json").write_text(json.dumps(sentinel), encoding="utf-8")
    rc = exp.main(["--out", str(out), "--part-size", "20", "--db", str(tmp_path / "skills.db")])
    assert rc == 0
    body = json.loads((out / "part-001.json").read_text(encoding="utf-8"))
    assert body.get("sentinel") is True  # 已有分片不覆盖


def test_named_candidates_not_exported(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-named", sha="sha-1", suggested_name="already-named")
    _insert_candidate(skills_db, "m-raw", sha="sha-2")
    out = tmp_path / "naming"
    rc = exp.main(["--out", str(out), "--part-size", "20", "--db", str(tmp_path / "skills.db")])
    assert rc == 0
    parts = _read_parts(out)
    ids = [it["memory_id"] for p in parts.values() for it in p["items"]]
    assert ids == ["m-raw"]


def test_empty_pool_writes_nothing_exit_zero(skills_db, tmp_path):
    out = tmp_path / "naming"
    rc = exp.main(["--out", str(out), "--part-size", "20", "--db", str(tmp_path / "skills.db")])
    assert rc == 0
    assert not out.exists() or list(out.glob("part-*.json")) == []


def test_old_schema_without_suggested_name_column(tmp_path):
    """老库没有 suggested_name 列：只读连接不能迁移，导出必须容忍。"""
    conn = sqlite3.connect(tmp_path / "skills.db")
    conn.executescript(
        """
        CREATE TABLE skill_candidates (
          candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
          memory_id TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          category TEXT, tags TEXT,
          confidence REAL NOT NULL DEFAULT 0.0,
          status TEXT NOT NULL DEFAULT 'pending',
          reason TEXT, note TEXT, reviewer TEXT,
          source TEXT NOT NULL DEFAULT 'memory_route',
          content_sha256 TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reviewed_at TEXT);
        """
    )
    conn.execute(
        "INSERT INTO skill_candidates (memory_id, name, description, content, status,"
        " content_sha256, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("m-old", "exp-old0001", "老库候选", "---\nname: exp-old0001\n---\n\n正文",
         "accepted", "sha-old", "2026-11-15T00:00:00Z", "2026-11-15T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    out = tmp_path / "naming"
    rc = exp.main(["--out", str(out), "--part-size", "20", "--db", str(tmp_path / "skills.db")])
    assert rc == 0
    parts = _read_parts(out)
    assert [it["memory_id"] for p in parts.values() for it in p["items"]] == ["m-old"]


def test_only_unexported_skips_and_continues_numbering(skills_db, tmp_path):
    for i in range(3):
        _insert_candidate(skills_db, f"m-{i}", sha=f"sha-{i}")
    out = tmp_path / "naming"
    rc = exp.main(["--out", str(out), "--part-size", "2", "--db", str(tmp_path / "skills.db")])
    assert rc == 0
    assert sorted(_read_parts(out)) == ["part-001.json", "part-002.json"]
    # 新增候选后，--only-unexported 只导新面孔，编号接续
    _insert_candidate(skills_db, "m-new", sha="sha-new")
    rc = exp.main(["--out", str(out), "--part-size", "2", "--db", str(tmp_path / "skills.db"),
                   "--only-unexported"])
    assert rc == 0
    parts = _read_parts(out)
    assert sorted(parts) == ["part-001.json", "part-002.json", "part-003.json"]
    assert [it["memory_id"] for it in parts["part-003.json"]["items"]] == ["m-new"]
