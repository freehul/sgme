# -*- coding: utf-8 -*-
"""tests/test_skills_pr7.py：PR-7 迁移就绪改动测试（pattern 枚举 / scripts 收紧 / skip_limits）。"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest


GOOD_META = {
    "description": "测试用合法技能描述",
    "version": "1.0.0",
    "pattern": "manual",
    "category": "testing",
}
GOOD_BODY = "# 标题\n\n正文内容"


def _rec(name, content="", sha=None, **kw):
    from sgme.skills.indexer import SkillRecord

    c = content.strip()
    return SkillRecord(
        name=name,
        content=c,
        sha256=sha or hashlib.sha256(c.encode("utf-8")).hexdigest(),
        **kw,
    )


# ---------- pattern 枚举 ----------


class TestPatternEnum:
    def test_auto_and_manual_pass(self):
        from sgme.skills.gates import lint_skill

        for p in ("auto", "manual"):
            meta = dict(GOOD_META, pattern=p)
            assert lint_skill(meta, GOOD_BODY, "good-skill", set()) == [], p

    def test_freeform_pattern_rejected(self):
        """自由文本（如 unit-test/legacy）不再合法——枚举外拒绝。"""
        from sgme.skills.gates import lint_skill

        meta = dict(GOOD_META, pattern="unit-test")
        v = lint_skill(meta, GOOD_BODY, "good-skill", set())
        assert any("pattern" in x and ("auto" in x and "manual" in x) for x in v)

    def test_missing_pattern_allowed_after_b116(self):
        """B116（2026-08-28）起 pattern 放宽为可选——缺失不再被拒。

        枚举约束仍在：给了值就必须是 auto/manual（见 test_freeform_pattern_rejected）。
        """
        from sgme.skills.gates import lint_skill

        meta = {k: v for k, v in GOOD_META.items() if k != "pattern"}
        assert lint_skill(meta, GOOD_BODY, "good-skill", set()) == []


# ---------- scripts 规则收紧：目录实际存在才检查 ----------


class TestScriptsRuleTightened:
    def test_text_mention_without_dir_passes(self, tmp_path):
        """正文提及 scripts/xxx 但技能目录无 scripts/ 子目录 → 放行（文档性提及不拦）。"""
        from sgme.skills.gates import lint_skill

        body = "排查用 `scripts/aixm_incremental.sh`（外部路径，仅文档说明）"
        # skill_dir 不存在 → 不检查
        assert lint_skill(dict(GOOD_META), body, "good-skill", set(),
                          skill_dir=tmp_path / "nope") == []

    def test_real_assets_require_declaration(self, tmp_path):
        """技能目录真有 scripts/ 子目录且含被引用文件 → 未声明仍拦截。"""
        from sgme.skills.gates import lint_skill

        sd = tmp_path / "good-skill"
        (sd / "scripts").mkdir(parents=True)
        (sd / "scripts" / "run.py").write_text("print('x')", encoding="utf-8")
        body = "运行 `scripts/run.py` 完成任务"
        v = lint_skill(dict(GOOD_META), body, "good-skill", set(), skill_dir=sd)
        assert any("scripts" in x for x in v)
        # 声明后通过
        meta = dict(GOOD_META, scripts=["run.py"])
        assert lint_skill(meta, body, "good-skill", set(), skill_dir=sd) == []

    def test_declared_but_file_missing_fails(self, tmp_path):
        """目录存在但声明的文件缺失 → 拦截（防断链）。"""
        from sgme.skills.gates import lint_skill

        sd = tmp_path / "good-skill"
        (sd / "scripts").mkdir(parents=True)  # 目录在但 run.py 不在
        meta = dict(GOOD_META, scripts=["run.py"])
        v = lint_skill(meta, "用 scripts/run.py", "good-skill", set(), skill_dir=sd)
        assert any("不存在" in x or "覆盖" in x for x in v)

    def test_default_no_dir_backcompat(self):
        """不传 skill_dir（默认 None）→ 等价于目录不存在 → 文字提及放行（迁移路径兼容）。"""
        from sgme.skills.gates import lint_skill

        body = "表格里提到 scripts/foo.mjs 示例"
        assert lint_skill(dict(GOOD_META), body, "good-skill", set()) == []


# ---------- skip_limits：超限降级为警告 ----------


class TestSkipLimits:
    def _store(self, tmp_path):
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
        return str(repo)

    def test_over_8k_with_skip_limit_passes_with_warning(self, tmp_path):
        from sgme.skills.store import write_skill

        repo = self._store(tmp_path)
        big_body = "# 大手册\n\n" + ("内容行。\n\n" * 1200)  # >8K
        assert len(big_body.encode("utf-8")) > 8192
        r = write_skill("big-skill", dict(GOOD_META), big_body, [repo], skip_limits=True)
        assert r["ok"] is True
        assert any("8192" in w or "8K" in w or "超限" in w for w in r["warnings"])

    def test_over_8k_without_skip_still_rejected(self, tmp_path):
        from sgme.skills.store import write_skill

        repo = self._store(tmp_path)
        big_body = "# 大手册\n\n" + ("内容行。\n\n" * 1200)
        r = write_skill("big2", dict(GOOD_META), big_body, [repo])
        assert r["ok"] is False and r["code"] == "lint_failed"

    def test_enum_violation_not_relaxed_by_skip(self, tmp_path):
        """skip_limits 只放宽大小类限制；枚举/必填等语义违规仍拒绝。"""
        from sgme.skills.store import write_skill

        repo = self._store(tmp_path)
        bad_meta = dict(GOOD_META, pattern="whatever")
        r = write_skill("bad-pattern", bad_meta, GOOD_BODY, [repo], skip_limits=True)
        assert r["ok"] is False and r["code"] == "lint_failed"


# ---------- 迁移脚本：pattern 回填 ----------


class TestMigratePatternBackfill:
    def test_generated_frontmatter_has_manual_pattern(self, tmp_path):
        """迁移生成的 SKILL.md frontmatter 必带 pattern: manual。"""
        import sqlite3

        import migrate_wiki_skills as m

        db = tmp_path / "wiki.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY, title TEXT,"
            " content TEXT, category TEXT, tags TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO wiki_pages VALUES ('w1','skill:demo','# Demo\n\n正文内容','skill/c','[\"skill\"]','active')"
        )
        conn.commit()
        conn.close()
        pages = m.load_pages_from_db(Path(db))
        cand, _ = m.select_skill_pages(pages)
        draft = m.build_draft(cand[0])
        rendered = m.render_skill_md(draft)
        assert "pattern: manual" in rendered.split("---")[1]
