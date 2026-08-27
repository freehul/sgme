"""tests/test_split_oversize_skills.py：超 8K 技能拆分外置化脚本测试（ST-36 T-107）。

覆盖：
1. 正常拆分：超 8K 的 SKILL.md 多 ## 章节 → --apply 后入口 ≤8192、references/ 生成、frontmatter 追加 references 数组
2. 幂等跳过：已有 references/ 目录 → 重跑不重复写（main 层面跳过 + split_one 幂等不覆盖）
3. needs-manual：正文无二级标题可切 → status=needs-manual
4. dry-run：默认模式不写盘（注：main() 报告行 r['before']  KeyError，见 test_dry_run_crashes_on_report_bug）
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_script():
    """按路径加载 scripts/split_oversize_skills.py 为模块。"""
    spec = importlib.util.spec_from_file_location(
        "split_oversize_skills", SCRIPTS_DIR / "split_oversize_skills.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["split_oversize_skills"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()

FM = "---\nname: big-skill\nversion: 0.1.0\n---\n\n"
INTRO = "这是一段引言文字，位于首个二级标题之前，用于描述技能的整体用途和适用场景。\n\n"


def _make_section(title: str, min_bytes: int = 2000) -> str:
    """构造一个 ≥min_bytes 的 ## 章节（body text 足够长）。"""
    filler = "测试填充文字，" * (min_bytes // 20 + 10)
    return f"## {title}\n\n{filler}\n\n"


def _oversize_skill_md() -> str:
    """构造正文 >8192 字节的 SKILL.md（含 frontmatter + intro + 3 个大章节）。"""
    s1 = _make_section("配置与部署", 3000)
    s2 = _make_section("操作手册", 3000)
    s3 = _make_section("排障指南", 3000)
    return FM + INTRO + s1 + s2 + s3


def _setup_skill(root: Path, name: str, content: str) -> Path:
    """在 root 下创建 <name>/SKILL.md 并返回目录。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 正常拆分
# ---------------------------------------------------------------------------


def test_normal_split(tmp_path: Path):
    """超 8K 技能 split_one 后：入口 ≤8192、references/ 有文件、frontmatter 追加 references。"""
    skill_dir = _setup_skill(tmp_path, "big-skill", _oversize_skill_md())

    result = mod.split_one(skill_dir)

    assert result["status"] == "ok"
    assert result["before"] > mod.MAX_BODY_BYTES
    assert result["after"] <= mod.MAX_BODY_BYTES

    # references/ 目录已生成且有文件
    refs_dir = skill_dir / "references"
    assert refs_dir.is_dir()
    ref_files = list(refs_dir.glob("*.md"))
    assert len(ref_files) >= 1  # 至少外置 1 个章节
    assert result["refs"] is not None
    assert len(result["refs"]) >= 1

    # 入口文件字节验证
    entrance_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    entrance_bytes = len(entrance_text.encode("utf-8"))
    assert entrance_bytes <= mod.MAX_BODY_BYTES

    # frontmatter 追加了 references 数组
    assert "references:" in entrance_text
    # ⚠️ 防回归（2026-08-27 实锤）：references 必须在 frontmatter 内（--- 结束符之前），
    #    不能插到正文开头——insert(-1) 遇 CRLF+末尾空串会错位
    fm_m = re.match(r"\A(---\r?\n.*?\r?\n---\r?\n?)", entrance_text, re.S)
    assert fm_m is not None, "入口无 frontmatter"
    assert "references:" in fm_m.group(1), f"references 错位到正文: {entrance_text[:80]!r}"

    # 外置章节引用链接存在
    for ref_name in result["refs"]:
        assert f"(references/{ref_name})" in entrance_text

    # 外置文件内容非空
    for ref_name in result["refs"]:
        ref_path = refs_dir / ref_name
        assert ref_path.exists()
        assert ref_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# 幂等跳过
# ---------------------------------------------------------------------------


def test_idempotent_skip_main(tmp_path: Path):
    """main() 层面：两个超 8K 技能，其中一个已有 references/ → 跳过该技能、处理另一个。"""
    # 技能 A：有 references/（模拟已拆分），报告为 skip
    skill_a = _setup_skill(tmp_path, "skill-a", _oversize_skill_md())
    (skill_a / "references").mkdir()
    (skill_a / "references" / "dummy.md").write_text("占位", encoding="utf-8")

    # 技能 B：无 references/（待拆分），报告为 ok
    _setup_skill(tmp_path, "skill-b", _oversize_skill_md())

    report_path = tmp_path / "report.md"
    rc = mod.main(["--root", str(tmp_path), "--apply", "--report", str(report_path)])
    assert rc == 0

    report = report_path.read_text(encoding="utf-8")
    assert "skill-a" in report
    assert "skill-b" in report
    assert "SKIP" in report  # skill-a 被跳过

    # skill-b 成功拆分
    assert (skill_b_dir := tmp_path / "skill-b" / "references").is_dir()
    assert len(list(skill_b_dir.glob("*.md"))) >= 1

    # skill-a 的 references/ 内容未被修改
    assert (skill_a / "references" / "dummy.md").read_text(encoding="utf-8") == "占位"


# ---------------------------------------------------------------------------
# 两级目录（分类结构）
# ---------------------------------------------------------------------------


def test_nested_category_structure(tmp_path: Path):
    """本地库分类形态 <cat>/<skill>/SKILL.md：rglob 扫描应命中并拆分。"""
    cat = tmp_path / "software-development"
    cat.mkdir()
    skill_dir = _setup_skill(cat, "deep-skill", _oversize_skill_md())

    report_path = tmp_path / "report.md"
    rc = mod.main(["--root", str(tmp_path), "--apply", "--report", str(report_path)])
    assert rc == 0

    report = report_path.read_text(encoding="utf-8")
    assert "deep-skill" in report
    assert "SKIP" not in report

    # 拆分产物在分类目录内
    assert (skill_dir / "references").is_dir()
    entrance = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert len(entrance.encode("utf-8")) <= mod.MAX_BODY_BYTES
    fm_m = re.match(r"\A(---\r?\n.*?\r?\n---\r?\n?)", entrance, re.S)
    assert fm_m and "references:" in fm_m.group(1)


def test_split_one_second_call_still_ok(tmp_path: Path):
    """split_one 第二次调用不报错（references: 不重复追加，幂等跳过 frontmatter 追加）。"""
    skill_dir = _setup_skill(tmp_path, "big-skill", _oversize_skill_md())

    r1 = mod.split_one(skill_dir)
    assert r1["status"] == "ok"
    entrance1 = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    refs_count1 = entrance1.count("references:")

    r2 = mod.split_one(skill_dir)
    assert r2["status"] == "ok"
    entrance2 = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    refs_count2 = entrance2.count("references:")

    # frontmatter references: 不重复追加
    assert refs_count1 == refs_count2


# ---------------------------------------------------------------------------
# needs-manual
# ---------------------------------------------------------------------------


def test_needs_manual_no_headings(tmp_path: Path):
    """正文无二级标题 → status=needs-manual。"""
    no_heading_md = FM + "这是一段没有任何二级标题的技能描述文字。" * 300 + "\n"
    skill_dir = _setup_skill(tmp_path, "no-heading", no_heading_md)

    result = mod.split_one(skill_dir)

    assert result["status"] == "needs-manual"
    assert "二级标题" in result["message"]

    # 不应生成 references/
    assert not (skill_dir / "references").is_dir()


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def test_dry_run_no_write(tmp_path: Path):
    """默认 dry-run 模式：main() 不崩溃、不写盘、退出码 0、报告含技能名。"""
    _setup_skill(tmp_path, "big-skill", _oversize_skill_md())

    report_path = tmp_path / "report.md"
    rc = mod.main(["--root", str(tmp_path), "--report", str(report_path)])
    assert rc == 0

    report = report_path.read_text(encoding="utf-8")
    assert "big-skill" in report
    assert "DRY-RUN" in report

    # references/ 未创建（dry-run 不写盘）
    refs_dir = tmp_path / "big-skill" / "references"
    assert not refs_dir.exists()

    # SKILL.md 未被修改
    skill_text = (tmp_path / "big-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "references:" not in skill_text


def test_split_one_dry_run_not_called(tmp_path: Path):
    """split_one 不参与 dry-run——它是写盘函数；dry-run 逻辑仅在 main() 中。"""
    # 验证 split_one 始终写盘（它本身不做 dry-run 判断）
    skill_dir = _setup_skill(tmp_path, "big-skill", _oversize_skill_md())

    result = mod.split_one(skill_dir)
    assert result["status"] == "ok"
    # split_one 调用后 references/ 已创建
    assert (skill_dir / "references").is_dir()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def test_split_sections():
    """split_sections 按 ## 正确切块。"""
    body = "引言\n\n## 第一节\n\n内容一\n\n## 第二节\n\n内容二\n"
    sections = mod.split_sections(body)
    assert len(sections) == 2
    assert sections[0][0] == "第一节"
    assert sections[1][0] == "第二节"
    assert "## 第一节" in sections[0][1]
    assert "## 第二节" in sections[1][1]


def test_split_sections_no_h2():
    """无 ## 时返回空列表。"""
    assert mod.split_sections("纯文本无标题") == []


def test_slugify():
    """slugify 生成合法 kebab-case 名。"""
    used: set[str] = set()
    s = mod.slugify("配置与部署", 1, used)
    assert re.match(r"^[a-z0-9][a-z0-9-]*$", s) or s.startswith("section-")
    used.add(s)
    s2 = mod.slugify("配置与部署", 2, used)
    assert s2 != s  # 去重


def test_oversize_skill_md_actually_oversize():
    """构造数据确实超过 8192 字节正文。"""
    md = _oversize_skill_md()
    fm_m = mod.FM_RE.match(md)
    body = fm_m.group(2) if fm_m else md
    assert len(body.encode("utf-8")) > mod.MAX_BODY_BYTES
