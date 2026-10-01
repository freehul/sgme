# -*- coding: utf-8 -*-
"""hooks：上下文剪裁 + 铁律（任何异常不阻塞会话，退出码恒 0）。"""
import io
import json
import sys

from claude_sgme import hooks


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _isolate_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(hooks, "LOG_FILE", tmp_path / "hook.log")


class _BoomAdapter:
    @classmethod
    def from_env(cls):
        raise RuntimeError("key missing and state unreadable")


def test_compose_context_skips_empty_signals():
    text = hooks._compose_context(
        {
            "inject": {"content": [{"type": "text", "text": "用户画像：喜欢简洁"}]},
            "signals": {"content": [{"type": "text", "text": "[]"}]},
        }
    )
    assert "claude-code" in text and "喜欢简洁" in text
    assert "关怀信号" not in text


def test_compose_context_includes_real_signals_and_errors():
    text = hooks._compose_context(
        {
            "inject_error": "inject down",
            "signals": {"content": [{"type": "text", "text": '{"count": 2, "events": [1, 2]}'}]},
        }
    )
    assert "inject down" in text
    assert "关怀信号" in text


def test_run_unknown_event_exit_zero(monkeypatch, tmp_path):
    _isolate_logs(monkeypatch, tmp_path)
    _stdin(monkeypatch, "")
    assert hooks.run("nope") == 0


def test_run_swallows_adapter_errors(monkeypatch, tmp_path):
    _isolate_logs(monkeypatch, tmp_path)
    _stdin(monkeypatch, json.dumps({"session_id": "s1", "transcript_path": "missing.jsonl"}))
    monkeypatch.setattr(hooks, "SgmeAdapter", _BoomAdapter)
    assert hooks.run("stop") == 0
    log_text = (tmp_path / "hook.log").read_text(encoding="utf-8")
    assert "ERROR" in log_text


def test_run_tolerates_invalid_stdin(monkeypatch, tmp_path):
    _isolate_logs(monkeypatch, tmp_path)
    _stdin(monkeypatch, "not json at all")
    monkeypatch.setattr(hooks, "SgmeAdapter", _BoomAdapter)
    assert hooks.run("session-end") == 0
