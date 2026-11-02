"""T-402 uses 依赖门禁测试（TDD）。

四类门禁：
1. 悬空引用：目标必须在 skills 表且 status != 'tombstone'
2. 依赖环：新边加入后 DFS 检出环（A→B→A）
3. 自引用：uses 包含自身
4. 强依赖阻断：strong 入向引用阻止删除目标；weak 只告警

另覆盖：
- 只读校验（不改存量 skill_uses）
- 老库补 status/strength 列时存量数据不丢
- sync_index 写入口：校验失败时**一行都不写**（不得先写后校验）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sgme.data import db as db_mod
from sgme.data import skills_dao
from sgme.data.skill_gate import check_removal, validate_uses


# ---------- fixtures ----------


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect_skills(tmp_path)
    yield c
    c.close()


def _add(conn, name, uses=(), status=None, strengths=None):
    """造一条技能（可选 uses 边与状态）。"""
    skills_dao.upsert_skill(
        conn,
        {
            "name": name,
            "sha256": "sha-" + name,
            "description": f"技能 {name}",
            "tags": ["skill"],
            "content": f"# {name}\n正文",
        },
    )
    if status:
        skills_dao.set_skill_status(conn, name, status)
    if uses:
        skills_dao.replace_uses(conn, name, list(uses), strengths=strengths)
    conn.commit()


# ---------- 1. 悬空引用 ----------


class TestDanglingReference:
    def test_existing_target_passes(self, conn):
        _add(conn, "alpha")
        _add(conn, "beta")
        assert validate_uses(conn, "alpha", ["beta"]) == []

    def test_missing_target_rejected(self, conn):
        _add(conn, "alpha")
        errors = validate_uses(conn, "alpha", ["ghost-skill"])
        assert any("悬空引用: ghost-skill" in e for e in errors), errors
        assert any("alpha" in e for e in errors), errors

    def test_tombstoned_target_rejected(self, conn):
        _add(conn, "alpha")
        _add(conn, "beta", status="tombstone")
        errors = validate_uses(conn, "alpha", ["beta"])
        assert any("悬空引用: beta" in e for e in errors), errors

    def test_builtin_protocol_is_not_dangling(self, conn):
        """随包分发的 skill-registry-protocol 不在 skills 表，但不是悬空引用。"""
        _add(conn, "alpha")
        assert validate_uses(conn, "alpha", ["skill-registry-protocol"]) == []

    def test_validation_is_readonly(self, conn):
        """校验不得改动存量边（生产 15 条数据的保护）。"""
        _add(conn, "alpha")
        _add(conn, "beta")
        _add(conn, "gamma", uses=["beta"])
        before = conn.execute(
            "SELECT src, dst, strength FROM skill_uses ORDER BY src, dst"
        ).fetchall()
        validate_uses(conn, "alpha", ["beta", "ghost-skill"])
        after = conn.execute(
            "SELECT src, dst, strength FROM skill_uses ORDER BY src, dst"
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]


# ---------- 2. 依赖环 ----------


class TestDependencyCycle:
    def test_direct_cycle_rejected(self, conn):
        _add(conn, "alpha")
        _add(conn, "beta", uses=["alpha"])
        errors = validate_uses(conn, "alpha", ["beta"])
        assert any("依赖环" in e for e in errors), errors
        joined = "".join(errors)
        assert "alpha" in joined and "beta" in joined, errors
        assert "alpha→beta→alpha" in joined, errors

    def test_indirect_cycle_rejected(self, conn):
        _add(conn, "alpha")
        _add(conn, "beta", uses=["gamma"])
        _add(conn, "gamma", uses=["alpha"])
        errors = validate_uses(conn, "alpha", ["beta"])
        assert any("依赖环" in e for e in errors), errors
        joined = "".join(errors)
        assert "alpha→beta→gamma→alpha" in joined, errors

    def test_dag_passes(self, conn):
        _add(conn, "alpha")
        _add(conn, "beta", uses=["gamma"])
        _add(conn, "gamma")
        assert validate_uses(conn, "alpha", ["beta", "gamma"]) == []

    def test_replacing_edges_breaks_old_cycle(self, conn):
        """全量替换语义：新边集合不再成环 → 放行（旧环边在 name 名下被替换掉）。"""
        _add(conn, "alpha", uses=["beta"])
        _add(conn, "beta", uses=["alpha"])
        # alpha 改名后只依赖 gamma，旧 alpha→beta 边会被全量替换（DFS 不应再走）
        _add(conn, "gamma")
        assert validate_uses(conn, "alpha", ["gamma"]) == []


# ---------- 3. 自引用 ----------


class TestSelfReference:
    def test_self_reference_rejected(self, conn):
        _add(conn, "alpha")
        errors = validate_uses(conn, "alpha", ["alpha"])
        assert any("自引用" in e for e in errors), errors
        assert any("alpha" in e for e in errors), errors

    def test_self_reference_does_not_duplicate_as_cycle(self, conn):
        _add(conn, "alpha")
        errors = validate_uses(conn, "alpha", ["alpha"])
        assert not any("依赖环" in e for e in errors), errors


# ---------- 4. 强/弱依赖 ----------


class TestDependencyStrength:
    def test_strong_incoming_blocks_removal(self, conn):
        _add(conn, "alpha")
        _add(conn, "beta", uses=["alpha"], strengths={"alpha": "strong"})
        errors, warnings = check_removal(conn, "alpha")
        assert any("强依赖" in e and "beta" in e for e in errors), errors
        assert warnings == []

    def test_weak_incoming_only_warns(self, conn):
        _add(conn, "alpha")
        _add(conn, "beta", uses=["alpha"], strengths={"alpha": "weak"})
        errors, warnings = check_removal(conn, "alpha")
        assert errors == []
        assert any("弱依赖" in w and "beta" in w for w in warnings), warnings

    def test_mixed_strength_reports_both(self, conn):
        _add(conn, "alpha")
        _add(conn, "beta", uses=["alpha"], strengths={"alpha": "strong"})
        _add(conn, "gamma", uses=["alpha"], strengths={"alpha": "weak"})
        errors, warnings = check_removal(conn, "alpha")
        assert any("beta" in e for e in errors), errors
        assert any("gamma" in w for w in warnings), warnings


# ---------- 写入口：sync_index 校验通过才写 ----------


def _md(name, uses=()):
    """造 SKILL.md；uses 项为 (target, strength)。"""
    lines = [
        "---",
        f"name: {name}",
        f"description: 测试技能 {name}",
        "category: testing",
        "version: 1.0.0",
        "pattern: manual",
    ]
    if uses:
        lines.append("uses:")
        for target, strength in uses:
            if strength == "strong":
                lines.append(f"  - name: {target}")
                lines.append("    strength: strong")
            else:
                lines.append(f"  - {target}")
    lines += ["---", f"# {name}", "正文"]
    return "\n".join(lines)


def _write(root: Path, name, uses=()):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_md(name, uses), encoding="utf-8")


def _cfg(root: Path):
    return {"skills": {"enabled": True, "source_dirs": [str(root)], "budget": 40}}


def _sync(conn, root):
    from sgme.operations.skills import sync_index

    return sync_index(_cfg(root), conn, None, max_embed=0, embed=False)


class TestSyncIndexGate:
    def test_valid_batch_writes_edges_and_strength(self, conn, tmp_path):
        root = tmp_path / "src"
        root.mkdir()
        _write(root, "alpha", [("beta", "strong")])
        _write(root, "beta")

        r = _sync(conn, root)
        assert r.ok is True, r
        assert skills_dao.find_outgoing(conn, "alpha") == ["beta"]
        row = conn.execute(
            "SELECT strength FROM skill_uses WHERE src='alpha' AND dst='beta'"
        ).fetchone()
        assert row["strength"] == "strong"

    def test_dangling_rejected_before_any_write(self, conn, tmp_path):
        root = tmp_path / "src"
        root.mkdir()
        _write(root, "alpha", [("ghost-skill", "weak")])

        r = _sync(conn, root)
        assert r.ok is False
        assert r.error_code == "ERR_INVALID_ARGS"
        assert any("悬空引用: ghost-skill" in v for v in r.details["violations"])
        # 关键：校验没过 → skills / skill_uses 一行都不能写
        assert skills_dao.count_skills(conn) == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM skill_uses").fetchone()["n"] == 0

    def test_cycle_rejected_before_any_write(self, conn, tmp_path):
        root = tmp_path / "src"
        root.mkdir()
        _write(root, "alpha", [("beta", "weak")])
        _write(root, "beta", [("alpha", "weak")])

        r = _sync(conn, root)
        assert r.ok is False
        assert any("依赖环" in v for v in r.details["violations"])
        assert skills_dao.count_skills(conn) == 0

    def test_self_reference_rejected_before_any_write(self, conn, tmp_path):
        root = tmp_path / "src"
        root.mkdir()
        _write(root, "alpha", [("alpha", "weak")])

        r = _sync(conn, root)
        assert r.ok is False
        assert any("自引用" in v for v in r.details["violations"])
        assert skills_dao.count_skills(conn) == 0

    def test_strong_dependent_blocks_target_deletion(self, conn, tmp_path):
        root = tmp_path / "src"
        root.mkdir()
        _write(root, "alpha", [("beta", "strong")])
        _write(root, "beta")
        assert _sync(conn, root).ok is True

        # beta 从源目录删除 → alpha 仍以 strong 依赖它
        (root / "beta" / "SKILL.md").unlink()
        (root / "beta").rmdir()
        r = _sync(conn, root)
        assert r.ok is False
        assert any("强依赖" in v and "beta" in v for v in r.details["violations"])
        # 被阻断 → beta 不能被删（不得先删后报错）
        assert skills_dao.get_skill(conn, "beta") is not None
        assert skills_dao.find_outgoing(conn, "alpha") == ["beta"]

    def test_weak_dependent_warns_but_allows_deletion(self, conn, tmp_path):
        root = tmp_path / "src"
        root.mkdir()
        _write(root, "alpha", [("beta", "weak")])
        _write(root, "beta")
        assert _sync(conn, root).ok is True

        (root / "beta" / "SKILL.md").unlink()
        (root / "beta").rmdir()
        r = _sync(conn, root)
        assert r.ok is True, r
        assert r.data["deleted"] == 1
        assert any("弱依赖" in w and "beta" in w for w in r.data["uses_warnings"])
        assert skills_dao.get_skill(conn, "beta") is None

    def test_strength_change_syncs_without_body_change(self, conn, tmp_path):
        """只改 frontmatter uses.strength（body sha 不变）也要更新依赖图。"""
        root = tmp_path / "src"
        root.mkdir()
        _write(root, "alpha", [("beta", "weak")])
        _write(root, "beta")
        assert _sync(conn, root).ok is True

        _write(root, "alpha", [("beta", "strong")])  # 正文不变，仅标记变化
        r = _sync(conn, root)
        assert r.ok is True, r
        assert r.data["updated"] == 1
        row = conn.execute(
            "SELECT strength FROM skill_uses WHERE src='alpha' AND dst='beta'"
        ).fetchone()
        assert row["strength"] == "strong"

    def test_weak_edge_dropped_when_referrer_rewritten(self, conn, tmp_path):
        """弱依赖目标被删 + 引用方本次也更新 → 边不写回，库内不留悬空引用。"""
        root = tmp_path / "src"
        root.mkdir()
        _write(root, "alpha", [("beta", "weak")])
        _write(root, "beta")
        assert _sync(conn, root).ok is True

        alpha_md = root / "alpha" / "SKILL.md"
        alpha_md.write_text(alpha_md.read_text(encoding="utf-8") + "\n新增正文", encoding="utf-8")
        (root / "beta" / "SKILL.md").unlink()
        (root / "beta").rmdir()

        r = _sync(conn, root)
        assert r.ok is True, r
        assert r.data["updated"] == 1
        assert skills_dao.find_outgoing(conn, "alpha") == []
        assert conn.execute("SELECT COUNT(*) AS n FROM skill_uses").fetchone()["n"] == 0


# ---------- 存量数据保护（老库补列） ----------

_OLD_SCHEMA = """
CREATE TABLE skills (
  name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, name_seg TEXT,
  description TEXT NOT NULL DEFAULT '', description_seg TEXT,
  category TEXT, tags TEXT, version TEXT, pattern TEXT, source TEXT,
  origin_path TEXT, content TEXT NOT NULL, content_seg TEXT,
  content_len INTEGER NOT NULL DEFAULT 0, updated_at TEXT, synced_at TEXT);
CREATE TABLE skill_vectors (
  name TEXT PRIMARY KEY, embedding BLOB NOT NULL, model TEXT NOT NULL,
  dims INTEGER NOT NULL, embedded_at TEXT NOT NULL);
CREATE TABLE skill_uses (
  src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (src, dst));
CREATE TABLE skill_sync_meta (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""


def test_legacy_rows_survive_status_strength_migration(tmp_path):
    """老库补 status/strength 列：存量技能与 uses 边原样保留（默认 active/weak）。"""
    p = tmp_path / "skills.db"
    raw = sqlite3.connect(p)
    raw.executescript(_OLD_SCHEMA)
    raw.execute(
        "INSERT INTO skills (name, sha256, content) VALUES ('legacy-a', 's1', '正文')"
    )
    raw.execute(
        "INSERT INTO skills (name, sha256, content) VALUES ('legacy-b', 's2', '正文')"
    )
    raw.execute("INSERT INTO skill_uses (src, dst) VALUES ('legacy-a', 'legacy-b')")
    raw.commit()
    raw.close()

    conn = db_mod.connect_skills(tmp_path)
    try:
        row = conn.execute(
            "SELECT status FROM skills WHERE name='legacy-a'"
        ).fetchone()
        assert row["status"] == "active"
        edge = conn.execute(
            "SELECT strength FROM skill_uses WHERE src='legacy-a' AND dst='legacy-b'"
        ).fetchone()
        assert edge["strength"] == "weak"
        assert skills_dao.find_outgoing(conn, "legacy-a") == ["legacy-b"]
        assert validate_uses(conn, "legacy-a", ["legacy-b"]) == []
    finally:
        conn.close()
