"""tests/test_care_consumer.py：关怀消费方脚本测试（T-38）。

覆盖：幂等去重（本地状态防重复通知）、消费标记、check-only 不消费、
SGME 不可达静默降级、无信号静默。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# 脚本 import（scripts/ 非包，直接加载）
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import care_consumer  # noqa: E402


@pytest.fixture
def fake_sgme(monkeypatch, tmp_path):
    """mock requests → 假 SGME 响应；状态文件隔离到 tmp_path。"""
    calls: dict[str, Any] = {"scan": 0, "list": 0, "consume": []}
    signals: list[dict] = []

    class FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body, ensure_ascii=False)

    def _post(url, **kw):
        if url.endswith("/v1/admin/care/scan"):
            calls["scan"] += 1
            return FakeResp(200, {"scan": {"care_daily": 1}})
        if "/consume" in url:
            calls["consume"].append(url)
            return FakeResp(200, {"status": "consumed"})
        return FakeResp(404, {})

    def _get(url, **kw):
        calls["list"] += 1
        return FakeResp(200, {"signals": signals, "total": len(signals)})

    monkeypatch.setattr(care_consumer.requests, "post", _post)
    monkeypatch.setattr(care_consumer.requests, "get", _get)
    monkeypatch.setattr(care_consumer.requests, "RequestException", __import__("requests").RequestException)
    monkeypatch.setattr(care_consumer, "BASE", tmp_path)
    monkeypatch.setattr(care_consumer, "STATE_DIR", tmp_path / "data" / "care")
    monkeypatch.setattr(care_consumer, "STATE_FILE", tmp_path / "data" / "care" / "consumer_state.json")
    monkeypatch.setattr(care_consumer, "_load_key", lambda: "test-key")
    return calls, signals


def _sig(eid: str, stype: str = "care_daily", payload: dict | None = None) -> dict:
    return {
        "event_id": eid, "type": stype, "ts": "2026-08-13T10:00:00Z",
        "payload": json.dumps(payload or {"date": "2026-08-13"}, ensure_ascii=False),
    }


def test_no_signals_silent(fake_sgme, capsys):
    """无信号 → 无输出（静默）。"""
    calls, signals = fake_sgme
    rc = care_consumer.main()
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert calls["scan"] == 1
    assert calls["list"] == 1


def test_output_and_consume_with_flag(fake_sgme, capsys):
    """--consume：输出 JSON 行 + 兜底消费 + 本地状态记录。"""
    calls, signals = fake_sgme
    signals.append(_sig("e-1", "care_todo_due", {"content": "待办 A"}))
    rc = care_consumer.main(["--consume"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "care_todo_due" in out
    assert "待办 A" in out
    assert len(calls["consume"]) == 1
    # 本地状态已记录 → 二次运行不再输出（幂等）
    signals.clear()
    rc = care_consumer.main(["--consume"])
    assert capsys.readouterr().out == ""


def test_default_no_consume(fake_sgme, capsys):
    """默认（无 --consume）：只输出不消费不记录（消费权归活跃 agent）。"""
    calls, signals = fake_sgme
    signals.append(_sig("e-2"))
    rc = care_consumer.main()
    assert rc == 0
    assert capsys.readouterr().out != ""
    assert calls["consume"] == []
    # 状态文件未写
    assert not care_consumer.STATE_FILE.exists()


def test_check_only_no_consume(fake_sgme, capsys):
    """--check-only：只读（向后兼容，等价默认）。"""
    calls, signals = fake_sgme
    signals.append(_sig("e-3"))
    rc = care_consumer.main(["--check-only"])
    assert rc == 0
    assert capsys.readouterr().out != ""
    assert calls["consume"] == []
    assert not care_consumer.STATE_FILE.exists()


def test_sgme_unreachable_silent_degrade(fake_sgme, capsys):
    """SGME 不可达 → 静默降级（不阻塞宿主）。"""
    calls, signals = fake_sgme

    def _boom(*a, **kw):
        raise __import__("requests").RequestException("conn refused")

    import care_consumer as cc
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cc.requests, "post", _boom)
    monkeypatch.setattr(cc, "_load_key", lambda: "test-key")
    try:
        rc = cc.main()
    finally:
        monkeypatch.undo()
    assert rc == 0
    assert "SGME 不可达" in capsys.readouterr().err


def test_missing_key_skips(fake_sgme, capsys, monkeypatch):
    """无 key → 跳过（stderr 提示，不崩溃）。"""
    monkeypatch.setattr(care_consumer, "_load_key", lambda: "")
    rc = care_consumer.main()
    assert rc == 0
    assert "未配置" in capsys.readouterr().err
