"""tests/test_skills_store.py：ST-36 M3 写入编排层 测试（TDD）。

真实 tmp 目录 + git init 的仓库上走全流程：
1. write_skill：lint 通过→落盘 <source_dir>/<name>/SKILL.md→git add+commit；
   lint 违规拒绝且不落盘；同名查重拒绝
2. remove_skill：入向引用（uses）一级信号拒绝并列清单；force 放行；
   二级信号（正文提及）只进 warnings 不拦；软删 = deprecated: true + commit；
   硬删 = 物理删目录 + commit；不存在 → ok=False
3. rename_skill：写新名副本 + 旧位置墓碑（superseded_by）+ commit；
   墓碑登记 tombstones.json（原子写）；新名已存在 → 拒绝；旧名不存在 → 拒绝

零真实网络：全部本地临时 git 仓库。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# ---------- fixture ----------


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    """真实 git 仓库（init + 提交身份 + 首个空提交），返回 source_dir。"""
    src = tmp_path / "skills_src"
    src.mkdir()
    _run_git(src, "init")
    _run_git(src, "config", "user.email", "test@sgme.local")
    _run_git(src, "config", "user.name", "SGME Test")
    (src / ".gitignore").write_text("*\n!*/\n!*/SKILL.md\n", encoding="utf-8")
    _run_git(src, "add", "-f", ".gitignore")
    _run_git(src, "commit", "-m", "chore: 初始化测试技能仓")
    return src


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path)


VALID_META = {
    "description": "合法技能描述",
    "version": "1.0.0",
    "pattern": "flow",
    "category": "testing",
}


def _write(repo: Path, name: str, meta=None, body="# 技能\n内容", **kw):
    from sgme.skills.store import write_skill

    return write_skill(name, dict(meta or VALID_META), body, [str(repo)], **kw)


def _read_skill(src: Path, name: str) -> str:
    return (src / name / "SKILL.md").read_text(encoding="utf-8")


# ---------- write_skill ----------


class TestWriteSkill:
    def test_write_ok_commits(self, repo):
        r = _write(repo, "alpha-skill")
        assert r["ok"] is True
        assert _read_skill(repo, "alpha-skill")  # 文件已落盘
        log = subprocess.run(
            ["git", "log", "--oneline", "-2"], cwd=str(repo),
            capture_output=True, text=True,
        )
        assert "alpha-skill" in log.stdout  # 有 commit

    def test_write_lint_violation_rejected_no_file(self, repo):
        r = _write(repo, "Bad_Name")
        assert r["ok"] is False and any("kebab" in v.lower() or "名称" in v for v in r["violations"])
        assert not (repo / "Bad_Name").exists()  # 未落盘

    def test_write_duplicate_name_rejected(self, repo):
        assert _write(repo, "dup-skill")["ok"]
        # 同名再写（同内容异名查重的同名分支）
        r2 = _write(repo, "dup-skill")
        assert r2["ok"] is False and any("同名" in v for v in r2["violations"])

    def test_write_same_content_different_name_rejected(self, repo):
        assert _write(repo, "first", body="完全相同的正文内容XYZ")["ok"]
        r = _write(repo, "second", body="完全相同的正文内容XYZ")
        assert r["ok"] is False and any("SHA" in v or "同内容" in v for v in r["violations"])

    def test_write_similar_content_warns_but_writes(self, repo):
        """语义近亲只警告不拦（warn_similar 进 warnings）。"""
        body_a = "# 向量近亲甲\n" + "独有段落甲。" * 50
        body_b = "# 向量近亲乙\n" + "独有段落乙。" * 50
        r1 = _write(repo, "sim-a", body=body_a)
        assert r1["ok"]
        r2 = _write(
            repo, "sim-b", body=body_b,
            query_vec=[1.0, 0.0], existing_vectors={"sim-a": [1.0, 0.0]},
        )
        # 近亲分数 1.0 ≥ 0.85 → 警告，但写入继续
        assert r2["ok"] is True and any("近亲" in w for w in r2["warnings"])

    def test_update_existing_overwrites(self, repo):
        """更新已有技能（覆盖写+commit）：同名查重只拦新建，不拦登记内更新。"""
        assert _write(repo, "upd-skill", body="v1 正文")["ok"]
        r = _write(repo, "upd-skill", body="v2 更新正文")
        assert r["ok"] is True
        assert "v2 更新正文" in _read_skill(repo, "upd-skill")


# ---------- remove_skill ----------


class TestRemoveSkill:
    def _setup_pair(self, repo: Path):
        """两个技能：beta 的 frontmatter uses 引用 alpha（一级），正文也提及（二级）。"""
        assert _write(repo, "alpha")["ok"]
        beta_meta = dict(VALID_META, uses=["alpha"])
        from sgme.skills.store import write_skill
        r = write_skill("beta", beta_meta, "依赖 alpha 完成部署", [str(repo)])
        assert r["ok"]

    def test_soft_delete_marks_deprecated_and_commits(self, repo):
        self._setup_pair(repo)
        # 先删 beta（无引用者），再验证 alpha 软删路径不受干扰
        from sgme.skills.store import remove_skill
        r = remove_skill("beta", source_dirs=[str(repo)])
        assert r["ok"] is True
        text = _read_skill(repo, "beta")
        assert "deprecated: true" in text  # 软删标记
        assert (repo / "beta" / "SKILL.md").exists()  # 目录仍在（软删）

    def test_inbound_uses_reference_blocks(self, repo):
        self._setup_pair(repo)
        from sgme.skills.store import remove_skill
        r = remove_skill("alpha", source_dirs=[str(repo)])
        assert r["ok"] is False
        assert any("beta" in ref for ref in r.get("referenced_by", []))  # 列出引用清单

    def test_force_removes_despite_references(self, repo):
        self._setup_pair(repo)
        from sgme.skills.store import remove_skill
        r = remove_skill("alpha", hard=True, force=True, source_dirs=[str(repo)])
        assert r["ok"] is True
        assert not (repo / "alpha").exists()  # 物理删除

    def test_body_mention_is_warning_only(self, repo):
        """二级信号（正文提及）只列 warnings 不拦。"""
        assert _write(repo, "gamma")["ok"]
        delta_meta = dict(VALID_META)
        from sgme.skills.store import write_skill, remove_skill
        assert write_skill("delta", delta_meta, "本技能与 gamma 无关但提到它", [str(repo)])["ok"]
        r = remove_skill("gamma", source_dirs=[str(repo)])
        assert r["ok"] is True  # 一级信号为空 → 不拦
        assert any("delta" in w for w in r["warnings"])  # 但清单里点名

    def test_hard_delete_physically_removes(self, repo):
        assert _write(repo, "victim")["ok"]
        from sgme.skills.store import remove_skill
        r = remove_skill("victim", hard=True, source_dirs=[str(repo)])
        assert r["ok"] is True and not (repo / "victim").exists()

    def test_missing_skill_fails(self, repo):
        from sgme.skills.store import remove_skill
        r = remove_skill("ghost", source_dirs=[str(repo)])
        assert r["ok"] is False


# ---------- rename_skill ----------


class TestRenameSkill:
    def test_rename_writes_new_and_tombstone(self, repo):
        assert _write(repo, "old-name")["ok"]
        from sgme.skills.store import rename_skill
        r = rename_skill("old-name", "new-name", source_dirs=[str(repo)])
        assert r["ok"] is True
        new_text = _read_skill(repo, "new-name")
        assert "合法技能描述" in new_text  # 新位置是完整副本
        tomb = _read_skill(repo, "old-name")
        assert "superseded_by: new-name" in tomb  # 墓碑指向新名
        # 墓碑登记文件
        reg = json.loads((Path(repo).parent / "tombstones.json").read_text(encoding="utf-8")) \
            if (Path(repo).parent / "tombstones.json").exists() else None
        # tombstones.json 默认落在 data/skills/ 下——由调用方传 registry_path 或默认相对 cwd/data/skills
        if reg is None:
            from sgme.skills.store import load_tombstones
            reg = load_tombstones()
        assert any(t["old"] == "old-name" and t["new"] == "new-name" for t in reg)

    def test_rename_missing_old_fails(self, repo):
        from sgme.skills.store import rename_skill
        r = rename_skill("ghost", "any-new", source_dirs=[str(repo)])
        assert r["ok"] is False

    def test_rename_to_existing_name_fails(self, repo):
        assert _write(repo, "a-one", body="# 甲\n内容甲")["ok"]
        assert _write(repo, "b-two", body="# 乙\n内容乙")["ok"]
        from sgme.skills.store import rename_skill
        r = rename_skill("a-one", "b-two", source_dirs=[str(repo)])
        assert r["ok"] is False


# ---------- 原子写 ----------


class TestAtomicJson:
    def test_tombstone_registry_roundtrip(self, tmp_path):
        from sgme.skills.store import append_tombstone, load_tombstones

        p = tmp_path / "data" / "skills" / "tombstones.json"
        append_tombstone({"old": "x", "new": "y"}, path=p)
        append_tombstone({"old": "a", "new": "b"}, path=p)
        got = load_tombstones(path=p)
        assert got == [{"old": "x", "new": "y"}, {"old": "a", "new": "b"}]

    def test_load_missing_returns_empty(self, tmp_path):
        from sgme.skills.store import load_tombstones

        assert load_tombstones(path=tmp_path / "nope.json") == []
