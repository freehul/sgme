"""install.py 测试（PR #3：settings.json 模板 + agent 注册）。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install  # noqa: E402


def test_generate_settings_json_contains_hooks():
    """模板包含 SessionStart/--start 与 SessionEnd/--end，命令指向项目 .venv。"""
    data = json.loads(install.generate_settings_json())
    assert "SessionStart" in data["hooks"]
    assert "SessionEnd" in data["hooks"]
    start_cmd = data["hooks"]["SessionStart"][0]["command"]
    end_cmd = data["hooks"]["SessionEnd"][0]["command"]
    assert "--start" in start_cmd
    assert "--end" in end_cmd
    # 命令必须是绝对路径且可执行（不依赖 shell PATH）
    assert ".venv/Scripts/python.exe" in start_cmd
    assert start_cmd.startswith("D:/") or start_cmd.startswith("C:/")


def test_write_settings_creates_file(tmp_path):
    target = tmp_path / "myproj"
    path = install.write_settings(target)
    assert path == target / ".reasonix" / "settings.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "SessionEnd" in data["hooks"]


def test_write_agents_md_creates_declaration(tmp_path):
    """AGENTS.md 生成：声明 SGME 存在（模型启动必加载 → 知情）。"""
    target = tmp_path / "myproj"
    path = install.write_agents_md(target)
    assert path == target / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    assert "SGME 记忆系统" in text
    assert "/sgme" in text


def test_write_agents_md_preserves_existing(tmp_path):
    """已有 AGENTS.md 时不覆盖，追加 SGME 段。"""
    target = tmp_path / "myproj"
    target.mkdir(parents=True)
    (target / "AGENTS.md").write_text("原有内容\n", encoding="utf-8")
    install.write_agents_md(target)
    text = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert "原有内容" in text
    assert "SGME 记忆系统" in text
    # 幂等：重复安装不重复追加
    install.write_agents_md(target)
    assert (target / "AGENTS.md").read_text(encoding="utf-8").count("SGME 记忆系统") == 1


def test_write_sgme_command(tmp_path):
    target = tmp_path / "myproj"
    path = install.write_sgme_command(target)
    assert path == target / ".reasonix" / "commands" / "sgme.md"
    text = path.read_text(encoding="utf-8")
    assert "description" in text
    assert "--query" in text


def test_register_agent_success(monkeypatch):
    import httpx

    class FakeResp:
        status_code = 200

        def json(self):
            return {"agent_id": "reasonix", "api_key": "agt_testkey123", "role": "agent"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(install, "httpx", httpx)
    monkeypatch.setattr(install, "_http", lambda: FakeClient())
    key = install.register_agent()
    assert key == "agt_testkey123"


def test_register_agent_failure_returns_none(monkeypatch):
    class FakeResp:
        status_code = 500

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(install, "_http", lambda: FakeClient())
    assert install.register_agent() is None


def test_save_key_writes_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(install, "ENV_FILE", env_file)
    install.save_key("agt_abc123")
    content = env_file.read_text(encoding="utf-8")
    assert "SGME_AGENT_KEY=agt_abc123" in content
