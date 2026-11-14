# -*- coding: utf-8 -*-
"""tests/test_apply_skill_names.py：T5c 命名导入脚本（2026-11-15）＋T10 三分类升级。

T5c 验收（任务清单-技能自动命名-20261115.md）：
1. 正常路径：写回 suggested_name / naming_model / named_at；默认 dry-run 零写入；
2. **串号检测**：names 里 memory_id 与输入该 i 的不符 → 该条 rejected；
3. 数量不符 → 该片整体 rejected；
4. 非法名（中文/大写/空格/超 48/``exp-`` 前缀）→ rejected；
5. 与 skills 主表撞名 → 自动加 ``-2``；
6. 本次导入内重名 → 后者加后缀；
7. 同 sha 的多条候选共用名（按 content_sha256 广播，dup_shares 计数）；
8. 已有 suggested_name 默认跳过，``--overwrite`` 才覆盖；
9. 老库（无命名三列）``--apply`` 时自动补列（幂等迁移）；
10. 孤儿 meta 文件（无配对 part）→ 跳过并报告，不写库。

T10 验收（任务清单-候选三分类-20261115.md）：
11. 输入改 ``part-XXX.meta.json``（name + kind + domain）；
12. 非法 kind（不在 skill/wiki/discard）→ rejected；
13. 非法 domain（不在 24 白名单 ∪ other，如 ``tech_stack``/中文）→ rejected；
14. 三列一并写：suggested_name / suggested_kind / suggested_domain；
15. 旧 ``*.names.json`` 被忽略并提示（不静默当合法输入）；
16. 汇总含 by_kind / by_domain 分布。

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
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / file_name)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


app = _load_script("apply_skill_names", "apply_skill_names.py")


def _add_naming_cols(conn: sqlite3.Connection) -> None:
    """给测试库补命名三列（T5c 落地后 connect_skills 自带，此处幂等兜底）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(skill_candidates)").fetchall()}
    for col in ("suggested_name", "naming_model", "named_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE skill_candidates ADD COLUMN {col} TEXT")
    conn.commit()


@pytest.fixture
def skills_db(tmp_path):
    from sgme.data import db as db_mod

    conn = db_mod.connect_skills(tmp_path)
    _add_naming_cols(conn)
    yield conn
    conn.close()


def _insert_candidate(
    conn: sqlite3.Connection, memory_id: str, *, sha: str | None = None,
    status: str = "accepted", suggested_name: str | None = None,
) -> None:
    now = "2026-11-15T00:00:00Z"
    conn.execute(
        "INSERT INTO skill_candidates (memory_id, name, description, content, category,"
        " tags, confidence, status, source, content_sha256, suggested_name,"
        " created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (memory_id, f"exp-{memory_id[:8]}", "测试候选", "---\nname: exp-x\n---\n\n正文",
         "testing", "[]", 0.95, status, "memory_route", sha, suggested_name, now, now),
    )
    conn.commit()


def _insert_skill(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO skills (name, sha256, content) VALUES (?,?,?)",
        (name, "sha-main", "正文"),
    )
    conn.commit()


def _write_part(naming_dir: Path, num: int, memory_ids: list[str], shas: list[str | None]):
    naming_dir.mkdir(parents=True, exist_ok=True)
    items = [
        {"i": i, "memory_id": mid, "sha": sha or "", "description": "d", "content_head": "c"}
        for i, (mid, sha) in enumerate(zip(memory_ids, shas))
    ]
    payload = {"part": num, "total_parts": 1, "part_size": 20, "items": items}
    (naming_dir / f"part-{num:03d}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _write_meta(
    naming_dir: Path, num: int, entries: list[dict],
    *, kind: str = "skill", domain: str = "other",
):
    """写 part-XXX.meta.json（T10 契约：name + kind + domain，缺省补合法值）。"""
    naming_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "part": num,
        "items": [
            {**e, "kind": e.get("kind", kind), "domain": e.get("domain", domain)}
            for e in entries
        ],
    }
    (naming_dir / f"part-{num:03d}.meta.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _row(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM skill_candidates WHERE memory_id=?", (memory_id,)
    ).fetchone()


# ---------- 正常路径 / dry-run ----------

def test_apply_writes_naming_fields(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    _insert_candidate(skills_db, "m-2", sha="sha-2")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1", "m-2"], ["sha-1", "sha-2"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "lmstudio-v1-prefix-404", "why": "w"},
        {"i": 1, "memory_id": "m-2", "name": "robocopy-symlink-broken", "why": "w"},
    ])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 2 and summary["rejected"] == 0
    for mid, expect in (("m-1", "lmstudio-v1-prefix-404"), ("m-2", "robocopy-symlink-broken")):
        row = _row(skills_db, mid)
        assert row["suggested_name"] == expect
        assert row["naming_model"] == "zcode-glm"
        assert row["named_at"]


def test_dry_run_writes_nothing(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1"], ["sha-1"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "valid-name-here", "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=False, overwrite=False)
    assert summary["applied"] == 0 and summary["would_apply"] == 1
    assert _row(skills_db, "m-1")["suggested_name"] is None


# ---------- 校验失败 ----------

def test_count_mismatch_rejects_whole_part(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    _insert_candidate(skills_db, "m-2", sha="sha-2")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1", "m-2"], ["sha-1", "sha-2"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "only-one-name", "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 0 and summary["rejected"] == 2
    assert _row(skills_db, "m-1")["suggested_name"] is None


def test_wrong_memory_id_rejects_item_only(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    _insert_candidate(skills_db, "m-2", sha="sha-2")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1", "m-2"], ["sha-1", "sha-2"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-OTHER", "name": "swapped-id-name", "why": "w"},  # 串号
        {"i": 1, "memory_id": "m-2", "name": "correct-item-name", "why": "w"},
    ])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 1 and summary["rejected"] == 1
    assert _row(skills_db, "m-1")["suggested_name"] is None
    assert _row(skills_db, "m-2")["suggested_name"] == "correct-item-name"


@pytest.mark.parametrize("bad", [
    "含中文名", "Upper-Case", "has space", "a" * 49, "exp-ab12cd34",
])
def test_invalid_names_rejected(skills_db, tmp_path, bad):
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1"], ["sha-1"])
    _write_meta(naming, 1, [{"i": 0, "memory_id": "m-1", "name": bad, "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 0 and summary["rejected"] == 1
    assert _row(skills_db, "m-1")["suggested_name"] is None


# ---------- 撞名与广播 ----------

def test_collision_with_skills_main_table_gets_suffix(skills_db, tmp_path):
    _insert_skill(skills_db, "taken-name")
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1"], ["sha-1"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "taken-name", "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 1 and summary["conflicts"] == 1
    assert _row(skills_db, "m-1")["suggested_name"] == "taken-name-2"


def test_internal_duplicate_gets_suffix(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    _insert_candidate(skills_db, "m-2", sha="sha-2")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1", "m-2"], ["sha-1", "sha-2"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "same-name-twice", "why": "w"},
        {"i": 1, "memory_id": "m-2", "name": "same-name-twice", "why": "w"},
    ])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 2 and summary["conflicts"] == 1
    assert _row(skills_db, "m-1")["suggested_name"] == "same-name-twice"
    assert _row(skills_db, "m-2")["suggested_name"] == "same-name-twice-2"


def test_same_sha_broadcast(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-main", sha="sha-dup")
    _insert_candidate(skills_db, "m-shadow", sha="sha-dup")  # 同正文另一条（不在分片里）
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-main"], ["sha-dup"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-main", "name": "shared-body-name", "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 1 and summary["dup_shares"] == 1
    assert _row(skills_db, "m-shadow")["suggested_name"] == "shared-body-name"
    assert _row(skills_db, "m-shadow")["naming_model"] == "zcode-glm"


# ---------- 已命名 / 老库 / 孤儿文件 ----------

def test_already_named_skipped_unless_overwrite(skills_db, tmp_path):
    _insert_candidate(skills_db, "m-1", sha="sha-1", suggested_name="old-name")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1"], ["sha-1"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "brand-new-name", "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 0 and summary["skipped_named"] == 1
    assert _row(skills_db, "m-1")["suggested_name"] == "old-name"
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=True)
    assert summary["applied"] == 1
    assert _row(skills_db, "m-1")["suggested_name"] == "brand-new-name"


def test_old_schema_migrated_on_apply(tmp_path):
    """老库无命名三列：--apply 自动补列后写入（幂等迁移）。"""
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
        CREATE TABLE skills (
          name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO skill_candidates (memory_id, name, status, content_sha256,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("m-old", "exp-old0001", "accepted", "sha-old",
         "2026-11-15T00:00:00Z", "2026-11-15T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-old"], ["sha-old"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-old", "name": "old-schema-name", "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 1
    conn = sqlite3.connect(tmp_path / "skills.db")
    row = conn.execute(
        "SELECT suggested_name, naming_model FROM skill_candidates WHERE memory_id='m-old'"
    ).fetchone()
    conn.close()
    assert row[0] == "old-schema-name" and row[1] == "zcode-glm"


def test_orphan_names_file_skipped(skills_db, tmp_path):
    naming = tmp_path / "naming"
    naming.mkdir(parents=True)
    _write_meta(naming, 9, [
        {"i": 0, "memory_id": "m-x", "name": "orphan-name", "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["parts"] == 0 and summary["applied"] == 0
    assert any("part-009" in detail for detail in summary["rejected_details"])


# ---------- T10：三分类（kind / domain） ----------

def test_meta_writes_three_columns(skills_db, tmp_path):
    """合法 meta：suggested_name / suggested_kind / suggested_domain 三列一并写。"""
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1"], ["sha-1"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "valid-meta-name", "why": "w",
         "kind": "wiki", "domain": "ai"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 1 and summary["rejected"] == 0
    row = _row(skills_db, "m-1")
    assert row["suggested_name"] == "valid-meta-name"
    assert row["suggested_kind"] == "wiki"
    assert row["suggested_domain"] == "ai"


def test_broadcast_shares_kind_and_domain(skills_db, tmp_path):
    """同 sha 广播行共用 name + kind + domain（同一正文同一分类）。"""
    _insert_candidate(skills_db, "m-main", sha="sha-dup")
    _insert_candidate(skills_db, "m-shadow", sha="sha-dup")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-main"], ["sha-dup"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-main", "name": "shared-triage-name", "why": "w",
         "kind": "discard", "domain": "tool"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 1 and summary["dup_shares"] == 1
    row = _row(skills_db, "m-shadow")
    assert row["suggested_kind"] == "discard" and row["suggested_domain"] == "tool"


@pytest.mark.parametrize("bad_kind", ["knowledge", "SKILL", ""])
def test_invalid_kind_rejected(skills_db, tmp_path, bad_kind):
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1"], ["sha-1"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "kind-check-name", "why": "w",
         "kind": bad_kind}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 0 and summary["rejected"] == 1
    assert _row(skills_db, "m-1")["suggested_kind"] is None


@pytest.mark.parametrize("bad_domain", ["tech_stack", "环境", "environment", ""])
def test_invalid_domain_rejected(skills_db, tmp_path, bad_domain):
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1"], ["sha-1"])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "domain-check-name", "why": "w",
         "domain": bad_domain}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 0 and summary["rejected"] == 1
    assert _row(skills_db, "m-1")["suggested_domain"] is None


def test_legacy_names_json_ignored_with_notice(skills_db, tmp_path):
    """旧 *.names.json 被忽略并提示（不静默当合法输入）。"""
    _insert_candidate(skills_db, "m-1", sha="sha-1")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-1"], ["sha-1"])
    # 旧格式（v2 遗留）：应被忽略
    (naming / "part-001.names.json").write_text(json.dumps({
        "part": 1,
        "names": [{"i": 0, "memory_id": "m-1", "name": "legacy-format-name", "why": "w"}],
    }, ensure_ascii=False), encoding="utf-8")
    # 新格式：正常收
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-1", "name": "new-format-name", "why": "w"}])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 1 and summary["rejected"] == 0
    assert summary["ignored_names_files"] == ["part-001.names.json"]
    assert _row(skills_db, "m-1")["suggested_name"] == "new-format-name"


def test_by_kind_by_domain_summary(skills_db, tmp_path):
    """汇总含 by_kind / by_domain 两维分布（按通过校验的条目计）。"""
    for i in range(4):
        _insert_candidate(skills_db, f"m-{i}", sha=f"sha-{i}")
    naming = tmp_path / "naming"
    _write_part(naming, 1, ["m-0", "m-1", "m-2", "m-3"],
                [f"sha-{i}" for i in range(4)])
    _write_meta(naming, 1, [
        {"i": 0, "memory_id": "m-0", "name": "name-a", "why": "w",
         "kind": "skill", "domain": "ai"},
        {"i": 1, "memory_id": "m-1", "name": "name-b", "why": "w",
         "kind": "wiki", "domain": "ai"},
        {"i": 2, "memory_id": "m-2", "name": "name-c", "why": "w",
         "kind": "wiki", "domain": "windows"},
        {"i": 3, "memory_id": "m-3", "name": "name-d", "why": "w",
         "kind": "discard", "domain": "tool"},
    ])
    summary = app.run(naming_dir=naming, db_path=tmp_path / "skills.db",
                      apply=True, overwrite=False)
    assert summary["applied"] == 4
    assert summary["by_kind"] == {"skill": 1, "wiki": 2, "discard": 1}
    assert summary["by_domain"] == {"ai": 2, "windows": 1, "tool": 1}


# ---------- db.py 列迁移（T5c 前置） ----------

def test_connect_skills_creates_naming_columns(tmp_path):
    """connect_skills 出来的新库自带命名三列（DDL）；老库经迁移函数补齐。"""
    from sgme.data import db as db_mod

    conn = db_mod.connect_skills(tmp_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(skill_candidates)").fetchall()}
        assert {"suggested_name", "naming_model", "named_at"} <= cols
    finally:
        db_mod.close(conn)
