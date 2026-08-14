"""bridge.py SessionStart 注入测试（PR #2：画像 + 相关记忆 → additionalContext）。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge  # noqa: E402

INJECT_BLOCKS = [
    {"title": "身份", "items": [{"content": "独立开发者"}]},
    {"title": "偏好", "items": [{"content": "中文交流，阮一峰风格"}]},
]

SEARCH_RESULTS = [
    {"content": "SGME 记忆引擎：L0→L1→L1.5→L2 提炼管线"},
    {"content": "Reasonix 接入 SGME：hooks 专用适配方案"},
]


@pytest.fixture(autouse=True)
def mock_sgme_api(monkeypatch):
    """mock SGME HTTP 调用（inject/search），保持单元测试离线。"""
    monkeypatch.setattr(bridge, "fetch_inject", lambda max_tokens=800: INJECT_BLOCKS)
    monkeypatch.setattr(bridge, "fetch_search", lambda query, limit=5: SEARCH_RESULTS)
    return monkeypatch


def test_build_start_context_includes_profile_and_memory():
    ctx = bridge.build_start_context(r"D:\Projects\SGME")
    assert "用户画像" in ctx
    assert "独立开发者" in ctx
    assert "相关记忆" in ctx
    assert "Reasonix 接入 SGME" in ctx
    # 身份说明：让模型知道自己在用 SGME 记忆系统
    assert "SGME 记忆系统" in ctx
    assert "/sgme" in ctx


def test_build_start_context_empty_on_failure(monkeypatch):
    """API 失败时：仍注入身份说明（知情不依赖 API），不含画像/记忆，不抛异常。"""
    monkeypatch.setattr(bridge, "fetch_inject", lambda max_tokens=800: [])
    monkeypatch.setattr(bridge, "fetch_search", lambda query, limit=5: [])
    ctx = bridge.build_start_context(r"D:\Projects\SGME")
    assert "SGME 记忆系统" in ctx
    assert "用户画像" not in ctx
    assert "相关记忆" not in ctx


def test_cmd_start_outputs_claude_compatible_json(capsys):
    """stdout 必须是 Claude Code 兼容的 hookSpecificOutput JSON。"""
    payload = {"event": "SessionStart", "cwd": r"D:\Projects\SGME"}
    rc = bridge.cmd_start(payload)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    hso = parsed["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert "独立开发者" in hso["additionalContext"]


def test_cmd_start_no_cwd_returns_empty(capsys):
    """缺 cwd 时静默（输出空），不抛异常。"""
    rc = bridge.cmd_start({})
    assert rc == 0
    assert capsys.readouterr().out == ""
