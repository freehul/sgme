"""import_history.py 测试（dsh 历史会话补导入）。

测试覆盖：
- L0 格式转换（to_l0）
- session 解析（parse_session_file）
- 幂等去重（get_existing_session_keys）
- append 调用（mock httpx）

不依赖真实 SGME 服务。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import import_history  # noqa: E402


def test_to_l0_user_message():
    """user 消息转 L0 格式（# {ts} user）。"""
    messages = [{"role": "user", "content": "你好", "ts": "2026-08-14T10:00:00Z"}]
    l0 = import_history.to_l0(messages)
    assert "# 2026-08-14T10:00:00Z user\n你好" in l0


def test_to_l0_assistant_message():
    """assistant 消息转 L0 格式（## {ts} assistant）。"""
    messages = [{"role": "assistant", "content": "回答", "ts": "2026-08-14T10:00:01Z"}]
    l0 = import_history.to_l0(messages)
    assert "## 2026-08-14T10:00:01Z assistant\n回答" in l0


def test_to_l0_tool_message():
    """tool 消息转 L0 格式（## {ts} tool + **tool**: name 前缀）。"""
    messages = [{"role": "tool", "content": '{"ok": true}', "name": "memory_search",
                 "ts": "2026-08-14T10:00:02Z"}]
    l0 = import_history.to_l0(messages)
    assert "## 2026-08-14T10:00:02Z tool\n**tool**: memory_search\n" in l0


def test_to_l0_multiple_messages():
    """多消息拼接正确。"""
    messages = [
        {"role": "user", "content": "问", "ts": "2026-08-14T10:00:00Z"},
        {"role": "assistant", "content": "答", "ts": "2026-08-14T10:00:01Z"},
    ]
    l0 = import_history.to_l0(messages)
    assert l0.count("\n\n") == 1  # 两个块之间一个空行分隔
    assert l0.endswith("\n")  # 末尾换行


def test_parse_session_file_filters_system(tmp_path):
    """解析过滤 system 消息。"""
    f = tmp_path / "session.jsonl"
    f.write_text(
        '{"role": "system", "content": "系统提示"}\n'
        '{"role": "user", "content": "用户消息", "createdAt": "2026-08-14T10:00:00Z"}\n',
        encoding="utf-8",
    )
    msgs = import_history.parse_session_file(f)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_parse_session_file_filters_empty_content(tmp_path):
    """解析过滤空内容消息。"""
    f = tmp_path / "session.jsonl"
    f.write_text(
        '{"role": "user", "content": "", "createdAt": "2026-08-14T10:00:00Z"}\n'
        '{"role": "assistant", "content": "有内容", "createdAt": "2026-08-14T10:00:01Z"}\n',
        encoding="utf-8",
    )
    msgs = import_history.parse_session_file(f)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "有内容"


def test_parse_session_file_skips_invalid_json(tmp_path):
    """解析跳过非法 JSON 行。"""
    f = tmp_path / "session.jsonl"
    f.write_text(
        'invalid json line\n'
        '{"role": "user", "content": "有效", "createdAt": "2026-08-14T10:00:00Z"}\n',
        encoding="utf-8",
    )
    msgs = import_history.parse_session_file(f)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "有效"


def test_discover_sessions_returns_empty_when_no_dir(tmp_path, monkeypatch):
    """会话目录不存在时返回空列表（不抛异常）。"""
    monkeypatch.setattr(import_history, "_DSH_HOME", tmp_path / "nonexistent")
    assert import_history.discover_sessions() == []


def test_append_to_sgme_success(monkeypatch):
    """append 成功返回 True。"""
    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append({"url": url, **kwargs})
            return FakeResp()

        def close(self):
            pass

    fake = FakeClient()
    monkeypatch.setattr(import_history, "_http", lambda: fake)
    result = import_history.append_to_sgme("l0 text", "dsh-test", "2026-08-14T10:00:00Z")
    assert result is True
    assert len(fake.posts) == 1
    payload = fake.posts[0]["json"]
    assert payload["session_key"] == "dsh-test"
    assert payload["agent_id"] == "dsh"
    assert payload["content"] == "l0 text"


def test_append_to_sgme_failure_returns_false(monkeypatch):
    """append 失败返回 False（不抛异常）。"""
    class FakeResp:
        status_code = 500

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(import_history, "_http", lambda: FakeClient())
    assert import_history.append_to_sgme("l0", "dsh-test", "ts") is False
