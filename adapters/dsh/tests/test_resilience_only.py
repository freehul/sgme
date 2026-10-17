"""故障自救卡落地的测试（--resilience-only 与 write_agents_md 两条路径）。

关键回归点：对**用户级**指令文件（手写纪律，无模板标记）只能追加指针段，
绝不能灌入 generate_agents_md() 的项目模板——否则会覆盖用户内容。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install  # noqa: E402

USER_LEVEL = """# 我的用户级指令

> 手写纪律，不得覆盖。

- 规则一：先计划后动手
- 规则二：原件不删
"""


def test_resilience_only_keeps_user_content(tmp_path):
    """用户级文件：原内容逐字保留，只多出指针段，且不含项目模板。"""
    agents = tmp_path / "AGENTS.md"
    agents.write_text(USER_LEVEL, encoding="utf-8")

    rc = install.main(["--dir", str(tmp_path), "--resilience-only"])

    assert rc == 0
    text = agents.read_text(encoding="utf-8")
    assert text.startswith(USER_LEVEL.rstrip()), "手写内容必须原样保留在文件开头"
    assert "SGME 故障自救" in text
    assert "SGME 记忆系统" not in text, "用户级文件不得注入项目模板"
    doc = tmp_path / ".sgme" / "sgme-resilience.md"
    assert doc.is_file() and doc.stat().st_size > 1000


def test_resilience_only_idempotent(tmp_path):
    """重跑不重复追加指针。"""
    agents = tmp_path / "AGENTS.md"
    agents.write_text(USER_LEVEL, encoding="utf-8")

    assert install.main(["--dir", str(tmp_path), "--resilience-only"]) == 0
    first = agents.read_text(encoding="utf-8")
    assert install.main(["--dir", str(tmp_path), "--resilience-only"]) == 0

    assert agents.read_text(encoding="utf-8") == first
    assert first.count("SGME 故障自救") == 1


def test_resilience_only_without_agents_file(tmp_path):
    """没有 AGENTS.md 时不新建（只落文档），避免凭空生成指令文件。"""
    rc = install.main(["--dir", str(tmp_path), "--resilience-only"])

    assert rc == 0
    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".sgme" / "sgme-resilience.md").is_file()


def test_write_agents_md_both_template_and_pointer(tmp_path):
    """项目目录路径不变：模板 + 指针都在。"""
    target = install.write_agents_md(tmp_path)

    text = target.read_text(encoding="utf-8")
    assert "SGME 记忆系统" in text
    assert "SGME 故障自救" in text
