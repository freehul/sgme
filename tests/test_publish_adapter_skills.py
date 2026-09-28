"""tests/test_publish_adapter_skills.py：A2 适配器分发打包脚本测试（零网络）。

覆盖：真源缺 SKILL.md 拒绝 / frontmatter name 改写 / junk 排除 / 幂等 / --check。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "publish_adapter_skills.py"


@pytest.fixture(scope="module")
def pub():
    spec = importlib.util.spec_from_file_location("publish_adapter_skills", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hosts_match_official_six(pub):
    assert set(pub.HOSTS) == {"hermes", "dsh", "doubao", "mimo", "workbuddy", "zcode"}


def test_rewrite_skill_md_name(pub):
    src = "---\nname: sgme\ndescription: x\n---\n# Body\n"
    out = pub._rewrite_skill_md(src, "workbuddy")
    assert "name: adapter-workbuddy" in out
    assert "# Body" in out
    assert "name: sgme\n" not in out.split("---")[1]


def test_rewrite_adds_frontmatter_when_missing(pub):
    out = pub._rewrite_skill_md("# Only body\n", "mimo")
    assert out.startswith("---\n")
    assert "name: adapter-mimo" in out
    assert "# Only body" in out


def test_publish_missing_skill_md_rejected(pub, tmp_path, monkeypatch):
    monkeypatch.setattr(pub, "ADAPTERS", tmp_path / "adapters")
    monkeypatch.setattr(pub, "SKILLS", tmp_path / "skills")
    (tmp_path / "adapters" / "ghost").mkdir(parents=True)
    r = pub.publish_host("ghost")
    assert r["ok"] is False and "SKILL.md" in r["error"]


def test_publish_excludes_junk_and_rewrites_name(pub, tmp_path, monkeypatch):
    adapters = tmp_path / "adapters"
    skills = tmp_path / "skills"
    monkeypatch.setattr(pub, "ADAPTERS", adapters)
    monkeypatch.setattr(pub, "SKILLS", skills)
    src = adapters / "workbuddy"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: sgme\ndescription: wb\n---\n# WB\n", encoding="utf-8"
    )
    (src / "install.py").write_text("# i\n", encoding="utf-8")
    (src / "scripts" / "sgme_client.py").write_text("# c\n", encoding="utf-8")
    (src / "README.md").write_text("# R\n", encoding="utf-8")
    (src / ".env").write_text("K=1\n", encoding="utf-8")
    (src / "tests").mkdir()
    (src / "tests" / "test_x.py").write_text("pass\n", encoding="utf-8")
    (src / "scripts" / "__pycache__").mkdir()
    (src / "scripts" / "__pycache__" / "a.pyc").write_bytes(b"x")

    r = pub.publish_host("workbuddy")
    assert r["ok"] is True
    dest = skills / "adapter-workbuddy"
    text = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert "name: adapter-workbuddy" in text
    assert (dest / "install.py").is_file()
    assert (dest / "scripts" / "sgme_client.py").is_file()
    assert (dest / "references" / "README.md").is_file()
    assert not (dest / ".env").exists()
    assert not (dest / "tests").exists()
    assert not (dest / "scripts" / "__pycache__").exists()

    # 幂等：重跑结果一致
    r2 = pub.publish_host("workbuddy")
    assert r2["ok"] is True and r2["files"] == r["files"]


def test_check_only_writes_nothing(pub, tmp_path, monkeypatch):
    adapters = tmp_path / "adapters"
    skills = tmp_path / "skills"
    monkeypatch.setattr(pub, "ADAPTERS", adapters)
    monkeypatch.setattr(pub, "SKILLS", skills)
    src = adapters / "mimo"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: mimo\n---\n# M\n", encoding="utf-8")
    r = pub.publish_host("mimo", check_only=True)
    assert r["ok"] is True and r["check_only"] is True
    assert "SKILL.md" in r["planned"]
    assert not (skills / "adapter-mimo").exists()


def test_real_repo_adapters_have_skill_md():
    """仓库真源六适配器均含 SKILL.md（打包前置）。"""
    for host in ("hermes", "dsh", "doubao", "mimo", "workbuddy", "zcode"):
        assert (ROOT / "adapters" / host / "SKILL.md").is_file(), host
