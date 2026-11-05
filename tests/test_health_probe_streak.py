"""tests/test_health_probe_streak.py：心跳 LLM 探测连续失败阈值（2026-11-05）。

验收：单次超时不再把 available 翻 false；连续 health_fail_threshold 次才判不可用；
恢复后清零。全部零网络（_probe_llm 被桩替换）。
"""

from __future__ import annotations

import pytest

from sgme.engine import health


@pytest.fixture(autouse=True)
def _reset():
    health.reset_llm_cache()
    yield
    health.reset_llm_cache()


def _cfg(threshold=2):
    return {
        "llm": {
            "chains": {"refinement": [{"provider": "agnes", "model": "agnes-2.5-flash", "base_url": "http://x"}]},
            "rules": {"primary_guard": {"enabled": True, "health_fail_threshold": threshold}},
        }
    }


def _good():
    return {"available": True, "provider": "agnes", "model": "agnes-2.5-flash", "error": None}


def _bad(err="超时: The read operation timed out"):
    return {"available": False, "provider": "agnes", "model": "agnes-2.5-flash", "error": err}


def test_single_failure_keeps_last_available(monkeypatch):
    seq = [_good(), _bad(), _bad(), _good()]
    monkeypatch.setattr(health, "_probe_llm", lambda cfg, client: dict(seq.pop(0)))
    cfg = _cfg(threshold=2)

    assert health._probe_with_streak(cfg)["available"] is True   # 首次成功
    assert health._probe_with_streak(cfg)["available"] is True   # 第 1 次失败 → 沿用上次可用
    assert health._probe_with_streak(cfg)["available"] is False  # 连续第 2 次 → 判不可用
    assert health._probe_with_streak(cfg)["available"] is True   # 恢复


def test_first_ever_failure_reports_unavailable(monkeypatch):
    monkeypatch.setattr(health, "_probe_llm", lambda cfg, client: _bad())
    data = health._probe_with_streak(_cfg(threshold=2))
    assert data["available"] is False


def test_threshold_one_flips_immediately(monkeypatch):
    seq = [_good(), _bad()]
    monkeypatch.setattr(health, "_probe_llm", lambda cfg, client: dict(seq.pop(0)))
    cfg = _cfg(threshold=1)
    assert health._probe_with_streak(cfg)["available"] is True
    assert health._probe_with_streak(cfg)["available"] is False


def test_reset_llm_cache_clears_streak(monkeypatch):
    seq = [_good(), _bad(), _good()]
    monkeypatch.setattr(health, "_probe_llm", lambda cfg, client: dict(seq.pop(0)))
    cfg = _cfg(threshold=2)
    health._probe_with_streak(cfg)   # good
    health._probe_with_streak(cfg)   # fail 1
    health.reset_llm_cache()         # 清空连败与 last_good
    assert health._probe_with_streak(cfg)["available"] is True  # good
