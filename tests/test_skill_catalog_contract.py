"""技能目录治理契约：命名、保留项和编码工作流入口。"""

import re
from pathlib import Path

import yaml

from sgme.skills.indexer import parse_skill_md

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED = {
    "sgme",
    "sgme-operations",
    "sgme-development",
    "sgme-adapter-development",
    "sgme-docs-authoring",
    "coding-workflow",
    "skill-governance",
    "sgme-model-keys",
}
ADAPTERS = {
    "adapter-claude-code",
    "adapter-codex",
    "adapter-doubao",
    "adapter-dsh",
    "adapter-hermes",
    "adapter-mimo",
    "adapter-workbuddy",
    "adapter-zcode",
}


def _skill_dirs() -> dict[str, Path]:
    return {
        path.name: path
        for path in SKILLS_ROOT.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }


def test_catalog_has_canonical_names_and_required_capabilities():
    skill_dirs = _skill_dirs()
    assert REQUIRED <= skill_dirs.keys()
    assert ADAPTERS <= skill_dirs.keys()
    assert "verify" not in skill_dirs
    assert "sgme-key" not in skill_dirs

    for name, directory in skill_dirs.items():
        assert NAME_RE.fullmatch(name), name
        parsed = parse_skill_md((directory / "SKILL.md").read_text(encoding="utf-8"))
        assert parsed["meta"].get("name") == name
        assert parsed["meta"].get("description"), name


def test_coding_workflow_covers_the_development_loop():
    text = (SKILLS_ROOT / "coding-workflow" / "SKILL.md").read_text(encoding="utf-8")
    for marker in ("TDD", "CodeGraph", "调试", "审查", "验证", "commit"):
        assert marker in text


def test_skill_governance_explains_dedupe_and_safe_lifecycle():
    text = (SKILLS_ROOT / "skill-governance" / "SKILL.md").read_text(encoding="utf-8")
    for marker in ("命名", "重复", "删除", "quick_validate", "materialize", "真源边界", "migration-preview"):
        assert marker in text


def test_default_skill_sources_cover_source_checkout_and_container_seed():
    """本地源码运行与 Docker 运行都必须能发现 canonical skills。

    只断言包内基线 ``sgme/resources/config/sgme.yaml``——它是唯一随包分发、
    入 git 的真相源；项目根 ``config/sgme.yaml`` 是 gitignore 的可写覆盖层
    （每台机器各一份），干净检出不存在，读它必挂 CI（T-220）。
    """
    config_path = ROOT / "sgme" / "resources" / "config" / "sgme.yaml"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    source_dirs = document["skills"]["source_dirs"]
    assert "./skills/" in source_dirs
    assert "/app/cache/skills/" in source_dirs
