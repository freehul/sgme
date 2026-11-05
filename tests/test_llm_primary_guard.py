"""tests/test_llm_primary_guard.py：首链熔断（2026-11-05）。

验收：
1. 首链连续失败达到阈值 → 打开熔断，**不再调用任何降级节点**（PrimaryUnavailable）
2. 熔断期内直接拒绝（不调 provider、不建真实连接）
3. 首链成功 → 计数清零、熔断解除
4. 未配置 primary_guard → 保持旧行为（继续降级）
5. 注入 client（测试形态）不触发熔断（避免跨用例污染）
6. 熔断告警载荷每次打开只可取一次

全部零真实网络：provider.get_provider 被替换为桩函数。
"""

from __future__ import annotations

import pytest

from sgme.llm import chain, provider


@pytest.fixture(autouse=True)
def _reset_guard():
    chain.reset_primary_guard()
    yield
    chain.reset_primary_guard()


class _DummyClient:
    """注入 client 用（熔断在 client 注入时旁路，不需要真实连接）。"""


def _cfg(guard_enabled=True, threshold=2):
    rules = {
        "max_retries": 1,
        "fallback_on": ["timeout"],
        "primary_guard": {
            "enabled": guard_enabled,
            "failure_threshold": threshold,
            "cooldown_minutes": 60,
        },
    }
    return {
        "chains": {
            "refinement": [
                {"provider": "p1"},
                {"provider": "p2"},
                {"provider": "rule", "rule": "drop_batch"},
            ]
        },
        "rules": rules,
    }


def _install_providers(monkeypatch, p1_ok=False):
    calls = {"p1": 0, "p2": 0, "rule": 0}
    state = {"p1_ok": p1_ok}

    def fake_get_provider(name, provider_type=None):
        if name == "p1":
            def fn(*a, **k):
                calls["p1"] += 1
                if state["p1_ok"]:
                    return "primary-ok", {}
                raise provider.LLMError("timeout", error_type="timeout")
            return fn
        if name == "p2":
            def fn(*a, **k):
                calls["p2"] += 1
                return "fallback-ok", {}
            return fn
        if name == "rule":
            def fn(*a, **k):
                calls["rule"] += 1
                raise provider.LLMUnavailable()
            return fn
        raise AssertionError(f"unexpected provider {name}")

    monkeypatch.setattr(provider, "get_provider", fake_get_provider)
    return calls, state


def test_guard_opens_after_threshold_and_skips_fallback(monkeypatch):
    calls, _ = _install_providers(monkeypatch)
    cfg = _cfg(threshold=2)

    # 第 1 次：首链失败 → 允许降级一次（fails=1）
    text, used, _ = chain.call_with_fallback(cfg, "prompt")
    assert text == "fallback-ok" and used == "p2"
    assert calls["p2"] == 1
    assert chain.primary_guard_state("refinement")["fails"] == 1

    # 第 2 次：首链失败达阈值 → 熔断，不再调用 p2/rule
    with pytest.raises(provider.PrimaryUnavailable):
        chain.call_with_fallback(cfg, "prompt")
    assert calls["p2"] == 1 and calls["rule"] == 0
    assert chain.is_primary_open("refinement") is True

    alert = chain.take_primary_alert("refinement")
    assert alert is not None
    assert alert["action"] == "primary_circuit_open"
    assert alert["consecutive_failures"] == 2
    assert alert["provider"] == "p1"
    # 载荷只发一次
    assert chain.take_primary_alert("refinement") is None

    # 第 3 次：熔断期内直接拒绝，provider 调用数不再增长
    with pytest.raises(provider.PrimaryUnavailable):
        chain.call_with_fallback(cfg, "prompt")
    assert calls["p1"] == 2  # 前两次调首链；第三次被熔断短路（未调 p1）
    assert calls["p2"] == 1 and calls["rule"] == 0


def test_primary_success_resets_counter( monkeypatch):
    calls, state = _install_providers(monkeypatch)
    cfg = _cfg(threshold=2)

    # 先失败一次（fails=1）
    chain.call_with_fallback(cfg, "prompt")
    assert chain.primary_guard_state("refinement")["fails"] == 1

    # 首链恢复 → 计数清零
    state["p1_ok"] = True
    text, used, _ = chain.call_with_fallback(cfg, "prompt")
    assert used == "p1" and text == "primary-ok"
    st = chain.primary_guard_state("refinement")
    assert st["fails"] == 0 and st["open"] is False


def test_guard_disabled_keeps_legacy_fallback(monkeypatch):
    calls, _ = _install_providers(monkeypatch)
    cfg = _cfg(guard_enabled=False, threshold=2)
    # 连续 3 次首链失败都应继续降级（旧行为），不抛 PrimaryUnavailable
    for _ in range(3):
        text, used, _ = chain.call_with_fallback(cfg, "prompt")
        assert text == "fallback-ok" and used == "p2"
    assert calls["p2"] == 3
    assert chain.is_primary_open("refinement") is False
    assert chain.primary_guard_state("refinement")["fails"] == 0


def test_injected_client_bypasses_guard(monkeypatch):
    calls, _ = _install_providers(monkeypatch)
    cfg = _cfg(threshold=1)  # 阈值 1：若生效，第二次必熔断
    cli = _DummyClient()
    for _ in range(3):
        text, used, _ = chain.call_with_fallback(cfg, "prompt", client=cli)
        assert text == "fallback-ok" and used == "p2"
    assert calls["p2"] == 3
    assert chain.is_primary_open("refinement") is False


def test_open_circuit_does_not_create_client(monkeypatch):
    """熔断期的拒绝必须发生在建 client 之前（不泄漏连接）。"""
    cfg = _cfg(threshold=1)
    # 预置熔断状态
    chain._GUARD_STATES["refinement"] = {
        "fails": 1, "open_until": __import__("time").monotonic() + 600,
        "last_error": "timeout", "alert": None,
    }
    with pytest.raises(provider.PrimaryUnavailable):
        chain.call_with_fallback(cfg, "prompt")
