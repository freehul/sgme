"""install.py 测试（dsh 适配器安装引导）。

- AGENTS.md 生成与幂等
- agent 注册（mock httpx）
- key 写入 .env

不依赖真实 SGME 服务。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install  # noqa: E402


def test_generate_agents_md_contains_sgme_declaration():
    """AGENTS.md 模板包含 SGME 声明 + 工具说明 + 历史导入。"""
    text = install.generate_agents_md()
    assert "SGME 记忆系统" in text
    assert "memory_search" in text
    assert "wiki_search" in text
    assert "/sgme" in text
    assert "历史会话补导入" in text


def test_write_agents_md_creates_declaration(tmp_path):
    """AGENTS.md 生成：声明 SGME 存在（模型启动必加载 → 知情）。"""
    target = tmp_path / "myproj"
    path = install.write_agents_md(target)
    assert path == target / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    assert "SGME 记忆系统" in text
    assert "memory_search" in text


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


def test_register_agent_success(monkeypatch):
    """agent 注册成功返回明文 key。"""
    class FakeResp:
        status_code = 200

        def json(self):
            return {"agent_id": "dsh", "api_key": "agt_testkey123", "role": "agent"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(install, "_http", lambda: FakeClient())
    key = install.register_agent()
    assert key == "agt_testkey123"


def test_register_agent_failure_returns_none(monkeypatch):
    """agent 注册失败返回 None（不抛异常）。"""
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
    """key 写入 .env，包含 BASE_URL/AGENT_KEY/ADMIN_KEY 三段。"""
    env_file = tmp_path / ".env"
    monkeypatch.setattr(install, "ENV_FILE", env_file)
    monkeypatch.setattr(install, "_BASE_URL", "http://127.0.0.1:9910")
    monkeypatch.setattr(install, "os", install.os)
    monkeypatch.setenv("SGME_ADMIN_KEY", "adm_test123")
    install.save_key("agt_abc123")
    content = env_file.read_text(encoding="utf-8")
    assert "SGME_AGENT_KEY=agt_abc123" in content
    assert "SGME_BASE_URL=http://127.0.0.1:9910" in content
    assert "SGME_ADMIN_KEY=adm_test123" in content


def test_save_key_preserves_other_lines(tmp_path, monkeypatch):
    """已有 .env 时保留其他非 key 行。"""
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER_VAR=value\nSGME_AGENT_KEY=old_key\n", encoding="utf-8")
    monkeypatch.setattr(install, "ENV_FILE", env_file)
    monkeypatch.setattr(install, "_BASE_URL", "http://127.0.0.1:9910")
    monkeypatch.setenv("SGME_ADMIN_KEY", "adm_test123")
    install.save_key("agt_new123")
    content = env_file.read_text(encoding="utf-8")
    assert "OTHER_VAR=value" in content
    assert "agt_new123" in content
    assert "old_key" not in content  # 旧 key 被替换
