"""tests/test_skills_gates.py：ST-36 M3 准入门禁 + 三层查重 + 进程内写锁 测试（TDD）。

每条门禁规则一拒一过：
1. frontmatter 必填（description/version/pattern/category 非空）
2. 触发词窗口（triggers 每项必须落在 description 前 57 字符内）
3. 原子 ≤8K（body UTF-8 字节数 ≤ 8192）
4. 名称 kebab-case 且全库唯一
5. scripts 声明（正文引用 scripts/<file> 时 meta.scripts 必须非空且覆盖）
6. uses 合法性（每项过 validate_name 且不等于自身）
7. 多违规累积
三层查重：同名拒绝 / 同 SHA 异名拒绝 / 语义近亲警告（向量可用才判，宁缺勿误报）/ 无冲突放行。
写锁：write_critical 临界区互斥（多线程计数精确）。
"""
from __future__ import annotations

import hashlib
import threading

import pytest

# ---------- 测试基线 ----------

GOOD_META = {
    "description": "测试用合法技能描述",
    "version": "1.0.0",
    "pattern": "unit-test",
    "category": "testing",
}
GOOD_BODY = "# 标题\n\n正文内容"


def _rec(name, content="", sha=None, **kw):
    """构造 SkillRecord 快捷方式（SHA 与 indexer 归一化一致：strip 后哈希）。"""
    from sgme.skills.indexer import SkillRecord

    c = content.strip()
    return SkillRecord(
        name=name,
        content=c,
        sha256=sha or hashlib.sha256(c.encode("utf-8")).hexdigest(),
        **kw,
    )


# ---------- 门禁：必填字段 ----------


class TestRequiredFields:
    def test_pass_full_meta(self):
        from sgme.skills.gates import lint_skill

        assert lint_skill(dict(GOOD_META), GOOD_BODY, "good-skill", set()) == []

    @pytest.mark.parametrize("field", ["description", "version", "pattern", "category"])
    def test_reject_missing_field(self, field):
        from sgme.skills.gates import lint_skill

        meta = {k: v for k, v in GOOD_META.items() if k != field}
        violations = lint_skill(meta, GOOD_BODY, "good-skill", set())
        assert any(field in v for v in violations), violations

    @pytest.mark.parametrize("field", ["description", "version", "pattern", "category"])
    def test_reject_empty_string_field(self, field):
        from sgme.skills.gates import lint_skill

        meta = dict(GOOD_META, **{field: "   "})
        violations = lint_skill(meta, GOOD_BODY, "good-skill", set())
        assert any(field in v for v in violations), violations


# ---------- 门禁：触发词 57 字符窗口 ----------


class TestTriggerWindow:
    def test_pass_trigger_inside_window(self):
        from sgme.skills.gates import lint_skill

        meta = dict(GOOD_META, triggers=["测试", "合法"])
        assert lint_skill(meta, GOOD_BODY, "good-skill", set()) == []

    def test_reject_trigger_outside_window(self):
        from sgme.skills.gates import lint_skill

        # 触发词「远窗」落在第 60+ 字符处，超出前 57 字符窗口
        desc = "x" * 60 + " 远窗触发词"
        meta = dict(GOOD_META, description=desc, triggers=["远窗触发词"])
        violations = lint_skill(meta, GOOD_BODY, "good-skill", set())
        assert any("触发词" in v for v in violations), violations

    def test_skip_when_no_triggers_field(self):
        """无 triggers 字段则跳过此条（不产生违规）。"""
        from sgme.skills.gates import lint_skill

        assert lint_skill(dict(GOOD_META), GOOD_BODY, "good-skill", set()) == []


# ---------- 门禁：原子 ≤8K ----------


class TestAtomicSize:
    def test_pass_exactly_8k(self):
        from sgme.skills.gates import lint_skill

        body = "a" * 8192  # 恰好 8192 字节 → 通过（边界）
        assert lint_skill(dict(GOOD_META), body, "good-skill", set()) == []

    def test_reject_over_8k(self):
        from sgme.skills.gates import lint_skill

        body = "a" * 8193
        violations = lint_skill(dict(GOOD_META), body, "good-skill", set())
        assert any("8192" in v or "8K" in v for v in violations), violations


# ---------- 门禁：名称 kebab-case + 全库唯一 ----------


class TestNameRule:
    def test_pass_kebab_unique(self):
        from sgme.skills.gates import lint_skill

        assert lint_skill(dict(GOOD_META), GOOD_BODY, "good-name-2", {"other"}) == []

    def test_reject_not_kebab(self):
        from sgme.skills.gates import lint_skill

        for bad in ("Bad_Name", "UPPER", "double--dash", "-lead", "trail-", "a b"):
            violations = lint_skill(dict(GOOD_META), GOOD_BODY, bad, set())
            assert any("kebab" in v.lower() or "名称" in v for v in violations), (bad, violations)

    def test_reject_duplicate_name(self):
        from sgme.skills.gates import lint_skill

        violations = lint_skill(dict(GOOD_META), GOOD_BODY, "taken", {"taken"})
        assert any("唯一" in v or "已存在" in v for v in violations), violations


# ---------- 门禁：scripts 声明 ----------


class TestScriptsDeclared:
    def test_pass_declared_and_covered(self):
        from sgme.skills.gates import lint_skill

        body = "# 用法\n运行 scripts/run_check.sh 与 scripts/lint.py"
        meta = dict(GOOD_META, scripts=["run_check.sh", "lint.py"])
        assert lint_skill(meta, body, "good-skill", set()) == []

    def test_reject_reference_without_scripts_meta(self):
        from sgme.skills.gates import lint_skill

        body = "执行 scripts/deploy.sh 完成部署"
        violations = lint_skill(dict(GOOD_META), body, "good-skill", set())
        assert any("scripts" in v for v in violations), violations

    def test_reject_partial_coverage(self):
        from sgme.skills.gates import lint_skill

        body = "执行 scripts/deploy.sh 与 scripts/verify.sh"
        meta = dict(GOOD_META, scripts=["deploy.sh"])  # 缺 verify.sh
        violations = lint_skill(meta, body, "good-skill", set())
        assert any("verify.sh" in v for v in violations), violations


# ---------- 门禁：uses 合法性 ----------


class TestUsesRule:
    def test_pass_valid_uses(self):
        from sgme.skills.gates import lint_skill

        meta = dict(GOOD_META, uses=["helper-skill"])
        assert lint_skill(meta, GOOD_BODY, "good-skill", set()) == []

    def test_reject_invalid_use_name(self):
        from sgme.skills.gates import lint_skill

        meta = dict(GOOD_META, uses=["../escape"])
        violations = lint_skill(meta, GOOD_BODY, "good-skill", set())
        assert any("uses" in v.lower() or "依赖" in v for v in violations), violations

    def test_reject_self_reference(self):
        from sgme.skills.gates import lint_skill

        meta = dict(GOOD_META, uses=["good-skill"])
        violations = lint_skill(meta, GOOD_BODY, "good-skill", set())
        assert any("自身" in v or "self" in v.lower() for v in violations), violations


# ---------- 门禁：多违规累积 ----------


class TestAccumulatedViolations:
    def test_multiple_violations_collected(self):
        from sgme.skills.gates import lint_skill

        meta = {"description": "", "category": "testing"}  # 缺 version/pattern
        violations = lint_skill(meta, "a" * 9000, "bad_name", set())
        assert len(violations) >= 3, violations


# ---------- 三层查重 ----------


class TestDedupe:
    def test_reject_same_name(self):
        from sgme.skills.dedupe import check_duplicate

        verdict = check_duplicate(_rec("dup", "内容A"), [_rec("dup", "内容B")])
        assert verdict == "reject_same_name"

    def test_reject_same_sha_different_name(self):
        from sgme.skills.dedupe import check_duplicate

        existing = [_rec("alpha", "共享正文")]
        new = _rec("beta", "共享正文")  # 同内容异名
        assert check_duplicate(new, existing) == "reject_same_sha"

    def test_warn_similar_with_vectors(self):
        from sgme.skills.dedupe import check_duplicate

        vec_a = [1.0, 0.0, 0.0]
        existing = [_rec("alpha", "向量技能甲")]
        new = _rec("beta", "向量技能乙")
        verdict = check_duplicate(
            new, existing, query_vec=vec_a, existing_vectors={"alpha": vec_a}
        )
        assert isinstance(verdict, tuple) and verdict[0] == "warn_similar"
        assert verdict[1] >= 0.99

    def test_no_similar_when_vectors_orthogonal(self):
        from sgme.skills.dedupe import check_duplicate

        existing = [_rec("alpha", "甲")]
        verdict = check_duplicate(
            _rec("beta", "乙"),
            existing,
            query_vec=[1.0, 0.0],
            existing_vectors={"alpha": [0.0, 1.0]},
        )
        assert verdict is None

    def test_skip_semantic_when_no_vectors(self):
        """向量不可用 → 跳过语义近亲层返回 None（宁缺勿误报）。"""
        from sgme.skills.dedupe import check_duplicate

        assert check_duplicate(_rec("beta", "任意"), [_rec("alpha", "甲")]) is None

    def test_none_when_all_clear(self):
        from sgme.skills.dedupe import check_duplicate

        assert check_duplicate(_rec("beta", "独有内容XYZ"), [_rec("alpha", "另一技能")]) is None

    def test_same_name_priority_over_sha(self):
        from sgme.skills.dedupe import check_duplicate

        verdict = check_duplicate(_rec("dup", "同文"), [_rec("dup", "同文")])
        assert verdict == "reject_same_name"


# ---------- 进程内写锁 ----------


class TestWriteCritical:
    def test_lock_is_threading_lock(self):
        from sgme.skills.writesync import write_lock

        assert isinstance(write_lock, type(threading.Lock()))

    def test_critical_section_mutual_exclusion(self):
        """8 线程各累加 200 次：临界区内互斥 → 计数精确等于 1600（无锁必有丢失更新）。"""
        from sgme.skills.writesync import write_critical

        counter = {"n": 0}

        def worker():
            for _ in range(200):
                with write_critical():
                    v = counter["n"]
                    counter["n"] = v + 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert counter["n"] == 1600
