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


def _git_head(cwd: Path) -> str:
    """取 HEAD 短哈希（T-221 守卫用例断言「无新提交」用）。"""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(cwd), check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


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
    "pattern": "manual",  # PR-7：pattern 枚举化（auto/manual）
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
            capture_output=True, text=True, encoding="utf-8",
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

    def test_write_metadata_only_change_allowed(self, repo):
        """T-123 实锤修复：仅 frontmatter 元数据变更（category）是合法更新，不得拒。

        「无变更重复提交」判定原只比 body sha——body 未变时 frontmatter 的
        category/pattern/tags 等变更被误判为重复提交（409）。
        """
        assert _write(repo, "meta-skill", meta={**VALID_META, "category": "testing"})["ok"]
        r = _write(repo, "meta-skill", meta={**VALID_META, "category": "network"})
        assert r["ok"] is True, r.get("violations")
        assert "category: network" in _read_skill(repo, "meta-skill")

    def test_write_metadata_tags_change_allowed(self, repo):
        """tags 变更同样放行（元数据变更族）。"""
        assert _write(repo, "tags-skill", meta={**VALID_META, "tags": ["skill"]})["ok"]
        r = _write(repo, "tags-skill", meta={**VALID_META, "tags": ["skill", "ops"]})
        assert r["ok"] is True, r.get("violations")

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

    def test_rename_updates_name_field_in_new_copy(self, repo):
        """T-221：新副本 frontmatter 的 name 字段同步为新名（名实一致）。"""
        assert _write(repo, "old-name", meta={**VALID_META, "name": "old-name"})["ok"]
        from sgme.skills.store import rename_skill
        r = rename_skill("old-name", "new-name", source_dirs=[str(repo)])
        assert r["ok"] is True
        assert "name: new-name" in _read_skill(repo, "new-name")


# ---------- 写入目录解析（source_dirs 多目录兜底） ----------


class TestWriteDirResolution:
    """write_skill 落盘目标解析：首项不合格时回退首个合格 git 仓（T-220）。

    背景：sgme.yaml 的 source_dirs 同时含源码运行目录（./skills/，非 git 仓）
    与 Docker 烘焙仓（/app/cache/skills/，git 仓）；写侧历史上盲取首项，
    首项非 git 仓时 mkdir 野目录后 StoreError。此处锁定回退语义（T-220）。
    """

    def test_first_dir_missing_falls_back_to_git_repo(self, tmp_path):
        from sgme.skills.store import write_skill

        repo = _make_repo(tmp_path)
        ghost = tmp_path / "nonexistent_dir"
        r = write_skill(
            "fallback-target", dict(VALID_META), "# 回退\n内容",
            [str(ghost), str(repo)],
        )
        assert r["ok"] is True, r
        assert _read_skill(repo, "fallback-target")  # 落到 git 仓
        assert not (ghost / "fallback-target").exists()  # 幽灵路径不落盘

    def test_first_dir_not_git_falls_back(self, tmp_path):
        from sgme.skills.store import write_skill

        repo = _make_repo(tmp_path)
        plain = tmp_path / "plain_dir"
        plain.mkdir()  # 存在但非 git 仓
        r = write_skill(
            "plain-first", dict(VALID_META), "# 甲\n内容甲",
            [str(plain), str(repo)],
        )
        assert r["ok"] is True, r
        assert _read_skill(repo, "plain-first")
        assert not (plain / "plain-first").exists()  # 非仓库不落野目录

    def test_no_valid_repo_raises_without_stray_dir(self, tmp_path):
        from sgme.skills.store import StoreError, write_skill

        plain_a = tmp_path / "a"
        plain_b = tmp_path / "b"
        plain_a.mkdir()
        plain_b.mkdir()
        with pytest.raises(StoreError):
            write_skill(
                "stray-check", dict(VALID_META), "# 乙\n内容乙",
                [str(plain_a), str(plain_b)],
            )
        # 失败不留野目录（历史上 mkdir 先于 git 校验，报错后残留）
        assert not (plain_a / "stray-check").exists()
        assert not (plain_b / "stray-check").exists()

    def test_first_valid_repo_still_wins(self, repo):
        from sgme.skills.store import write_skill

        # 首项本身是 git 仓 → 行为与旧版一致（向后兼容锚）
        r = write_skill("first-wins", dict(VALID_META), "# 丙\n内容丙",
                        [str(repo)])
        assert r["ok"] is True, r
        assert _read_skill(repo, "first-wins")


# ---------- 写侧独立仓守卫（T-221） ----------


class TestWriteSideRepoGuard:
    """删除/改名/资产写入须落在独立 git 技能仓（T-221）。

    背景：技能目录嵌在外层 git 仓库里时（本机源码运行 ./skills/ 形态），
    历史上动盘后才走 git——删除/改名的提交会落进外层仓、无关改动被
    ``git add -A`` 扫入；资产写入先落盘后报错留残。此处锁定「动盘前拦截、
    零副作用」。
    """

    def _nested_workspace(self, tmp_path: Path) -> Path:
        """外层 git 仓里的技能目录（自身无 .git）——模拟本机源码运行 ./skills/ 形态。"""
        outer = _make_repo(tmp_path)
        skills = outer / "skills"
        demo = skills / "demo-skill"
        demo.mkdir(parents=True)
        (demo / "SKILL.md").write_text(
            "---\ndescription: 嵌套形态测试技能\ncategory: testing\n---\n\n# 正文\n",
            encoding="utf-8",
        )
        _run_git(outer, "add", "-f", "skills/demo-skill/SKILL.md")
        _run_git(outer, "commit", "-m", "chore: 收编嵌套技能目录")
        return skills

    def test_remove_rejects_nested_dir_without_side_effects(self, tmp_path):
        from sgme.skills.store import StoreError, remove_skill

        skills = self._nested_workspace(tmp_path)
        outer = skills.parent
        before = _read_skill(skills, "demo-skill")
        head_before = _git_head(outer)
        with pytest.raises(StoreError):
            remove_skill("demo-skill", source_dirs=[str(skills)])
        # 零副作用：技能文件未动、外层仓无新提交（修复前：deprecated 入文件 +
        # 提交落进外层仓，还可能把无关改动一起扫入）
        assert _read_skill(skills, "demo-skill") == before
        assert _git_head(outer) == head_before

    def test_rename_rejects_nested_dir_without_side_effects(self, tmp_path):
        from sgme.skills.store import StoreError, rename_skill

        skills = self._nested_workspace(tmp_path)
        outer = skills.parent
        before = _read_skill(skills, "demo-skill")
        head_before = _git_head(outer)
        registry = tmp_path / "tombstones.json"
        with pytest.raises(StoreError):
            rename_skill("demo-skill", "demo-renamed", [str(skills)],
                         registry_path=registry)
        # 零副作用：无新副本、无墓碑、无登记、外层仓无新提交
        assert not (skills / "demo-renamed").exists()
        assert _read_skill(skills, "demo-skill") == before
        assert not registry.exists()
        assert _git_head(outer) == head_before

    def test_asset_write_rejects_nested_dir_without_stray_file(self, tmp_path):
        from sgme.skills.store import StoreError, write_skill_file

        skills = self._nested_workspace(tmp_path)
        with pytest.raises(StoreError):
            write_skill_file("demo-skill", "references/note.md", "hi", [str(skills)])
        # 修复前：先落盘后报错 → references/note.md 残留；修复后：动盘前拦截
        assert not (skills / "demo-skill" / "references").exists()


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
