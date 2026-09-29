"""SGME 三大核心能力入口契约测试。

这些测试锁定「memory / skills / wiki 是统一能力平面」的产品口径，
避免总手册、冷启动协议和 Agent onboarding 再次各说各话。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_core_sgme_skill_has_single_valid_frontmatter_and_three_modules():
    text = _read("skills/sgme/SKILL.md")
    lines = text.splitlines()
    assert lines[0] == "---"
    assert "---" in lines[1:]
    assert lines.index("---", 1) < 30

    from sgme.skills.indexer import parse_skill_md

    parsed = parse_skill_md(text)
    assert parsed["meta"]["name"] == "sgme"
    assert parsed["meta"]["category"] == "sgme"

    body = parsed["body"]
    for module in ("memory", "skills", "wiki"):
        assert module in body
    for operation in (
        "append",
        "inject",
        "search",
        "skill_search",
        "skill_get",
        "wiki_search",
        "wiki_page",
    ):
        assert operation in body


def test_skill_registry_protocol_routes_core_modules_before_specialist_skills():
    text = _read("sgme/skills/protocol/SKILL.md")
    assert "memory" in text
    assert "skills" in text
    assert "wiki" in text
    assert "不需要安装很多 skill" in text
    assert "skill_search" in text
    assert "skill_get" in text
    assert "不凭空编造" in text


def test_coldstart_protocol_explains_core_capability_plane():
    from sgme.operations.skills import cold_start

    result = cold_start({"skills": {"enabled": True}}, None)
    assert result.ok
    content = result.data["index"]["items"][0]["content"]
    for module in ("memory", "skills", "wiki"):
        assert module in content
    assert "skill_search" in content
