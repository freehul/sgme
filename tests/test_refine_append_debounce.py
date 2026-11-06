"""tests/test_refine_append_debounce.py：append 联动去抖（D-80，2026-11-06）。

背景：refine_on_append=true 时每次追加都触发一次提炼；块级失败零容忍下，每次触发
都要把整份未提交增量重提（实测同一文件 14.4h 内 23 轮 / 835 次调用）。去抖 = 窗口内
同文件只触发一次 + 增量不足阈值交给 batch_scan。
"""
from __future__ import annotations

import time

from sgme.engine import pipeline


class _FakeThread:
    started: list = []

    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target
        self.daemon = daemon
        _FakeThread.started.append(self)

    def start(self) -> None:  # 不真的跑目标函数（只验证是否被触发）
        return None


def _cfg(**overrides):
    refine = {
        "refine_on_append": True,
        "append_debounce_s": 300,
        "append_min_new_messages": 20,
    }
    refine.update(overrides)
    return {"refine": refine}


def _reset():
    _FakeThread.started = []
    pipeline._APPEND_REFINE_LAST.clear()


def test_small_append_skips_trigger(monkeypatch):
    _reset()
    monkeypatch.setattr(pipeline.threading, "Thread", _FakeThread)
    pipeline._maybe_refine_on_append(_cfg(), None, None, "f1", new_messages=3)
    assert _FakeThread.started == [], "少于阈值条数的追加不该触发（留给 batch_scan）"


def test_debounce_skips_second_trigger(monkeypatch):
    _reset()
    monkeypatch.setattr(pipeline.threading, "Thread", _FakeThread)
    cfg = _cfg()
    pipeline._maybe_refine_on_append(cfg, None, None, "f2", new_messages=50)
    assert len(_FakeThread.started) == 1
    pipeline._maybe_refine_on_append(cfg, None, None, "f2", new_messages=50)
    assert len(_FakeThread.started) == 1, "窗口内同文件第二次追加必须被去抖"


def test_trigger_after_window(monkeypatch):
    _reset()
    monkeypatch.setattr(pipeline.threading, "Thread", _FakeThread)
    cfg = _cfg()
    pipeline._APPEND_REFINE_LAST["f3"] = time.monotonic() - 301
    pipeline._maybe_refine_on_append(cfg, None, None, "f3", new_messages=50)
    assert len(_FakeThread.started) == 1, "窗口过去后应再次触发"


def test_disabled_switch_never_triggers(monkeypatch):
    _reset()
    monkeypatch.setattr(pipeline.threading, "Thread", _FakeThread)
    pipeline._maybe_refine_on_append(
        {"refine": {"refine_on_append": False}}, None, None, "f4", new_messages=100,
    )
    assert _FakeThread.started == []


def test_zero_thresholds_disable_gates(monkeypatch):
    """debounce=0 / min=0 = 关闭两道闸（保留旧行为，供需要即时提炼的部署）。"""
    _reset()
    monkeypatch.setattr(pipeline.threading, "Thread", _FakeThread)
    cfg = _cfg(append_debounce_s=0, append_min_new_messages=0)
    pipeline._maybe_refine_on_append(cfg, None, None, "f5", new_messages=1)
    pipeline._maybe_refine_on_append(cfg, None, None, "f5", new_messages=1)
    assert len(_FakeThread.started) == 2
