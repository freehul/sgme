"""测试：LLM 调用层节流器（令牌桶，ST-23⑥）。

- TokenBucket 单元测试（假时钟，不真实等待）
- provider 集成：call_openai_compatible 按 rules.throttle 平滑请求速率
- 默认值：rps=0.5（≈30 req/min，常用云端限流 60 req/min 的一半作安全余量）、
  burst=1（无突发，严格平滑）
"""
from __future__ import annotations

import time

import httpx
import pytest

from sgme.llm import provider
from sgme.llm.throttle import TokenBucket


# ---------- 假时钟 ----------

class FakeClock:
    """可控时钟：now 返回虚拟时间，sleep 推进虚拟时间。"""

    def __init__(self, t: float = 0.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture(autouse=True)
def _isolate_throttle(monkeypatch):
    """节流器每测重置；真实 sleep 替换为记录器（集成测试不真实等待）。"""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    provider.reset_throttle()
    yield sleeps
    provider.reset_throttle()


# ---------- TokenBucket 单元测试 ----------

def test_bucket_first_acquire_immediate():
    """满令牌首次获取立即通过。"""
    fc = FakeClock()
    b = TokenBucket(rate=1.0, capacity=1.0, now=fc.now, sleeper=fc.sleep)
    assert b.acquire() == 0.0


def test_bucket_second_acquire_waits_min_interval():
    """rate=0.5, cap=1 → 连续两次调用间隔 2s（0.5 rps = 每 2s 一次）。"""
    fc = FakeClock()
    b = TokenBucket(rate=0.5, capacity=1.0, now=fc.now, sleeper=fc.sleep)
    assert b.acquire() == 0.0
    wait = b.acquire()
    assert wait == pytest.approx(2.0)
    assert fc.t == pytest.approx(2.0)


def test_bucket_refills_over_time():
    """间隔足够则令牌恢复，无需等待。"""
    fc = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, now=fc.now, sleeper=fc.sleep)
    b.acquire()
    b.acquire()
    fc.t += 1.5  # 1.5s 后补充 1.5 个令牌（上限 2）
    assert b.acquire() == 0.0


def test_bucket_burst_capacity():
    """burst=3 → 连续 3 次立即可用，第 4 次等待 1s（rate=1）。"""
    fc = FakeClock()
    b = TokenBucket(rate=1.0, capacity=3.0, now=fc.now, sleeper=fc.sleep)
    assert b.acquire() == 0.0
    assert b.acquire() == 0.0
    assert b.acquire() == 0.0
    assert b.acquire() == pytest.approx(1.0)


def test_bucket_acquire_multiple_tokens():
    """一次获取多令牌 → 按缺口等待。"""
    fc = FakeClock()
    b = TokenBucket(rate=1.0, capacity=1.0, now=fc.now, sleeper=fc.sleep)
    assert b.acquire(tokens=2.0) == pytest.approx(1.0)


def test_bucket_invalid_params_raise():
    """rate/capacity 非正数 → ValueError。"""
    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1)
    with pytest.raises(ValueError):
        TokenBucket(rate=1, capacity=0)


# ---------- provider 集成（call_openai_compatible 节流） ----------

def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _ok_response(text: str = "ok") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


_NODE = {"base_url": "http://127.0.0.1:1/v1", "model": "m", "context_window": 65536}


def test_provider_throttle_disabled_no_wait(_isolate_throttle):
    """未配置 throttle → 不等待（默认关闭，行为不变）。"""
    sleeps = _isolate_throttle
    rules = {"timeout_s": 10}
    cli = _mock_client(lambda req: _ok_response())
    provider.call_openai_compatible("p", _NODE, rules, client=cli)
    provider.call_openai_compatible("p", _NODE, rules, client=cli)
    assert sleeps == []


def test_provider_throttle_enabled_enforces_min_interval(_isolate_throttle):
    """throttle.enabled=true（rps=0.5, burst=1）→ 连续两次调用第二次等待 ≥1.9s。"""
    sleeps = _isolate_throttle
    rules = {"timeout_s": 10, "throttle": {"enabled": True, "rps": 0.5, "burst": 1}}
    cli = _mock_client(lambda req: _ok_response())
    provider.call_openai_compatible("p", _NODE, rules, client=cli)
    provider.call_openai_compatible("p", _NODE, rules, client=cli)
    assert len(sleeps) == 1
    assert sleeps[0] >= 1.9


def test_provider_throttle_partial_config_uses_defaults(_isolate_throttle):
    """只配 enabled=true → 默认 rps=0.5 → 第二次等待约 2s。"""
    sleeps = _isolate_throttle
    rules = {"timeout_s": 10, "throttle": {"enabled": True}}
    cli = _mock_client(lambda req: _ok_response())
    provider.call_openai_compatible("p", _NODE, rules, client=cli)
    provider.call_openai_compatible("p", _NODE, rules, client=cli)
    assert len(sleeps) == 1
    assert 1.9 <= sleeps[0] <= 2.5


def test_provider_throttle_param_change_rebuilds(_isolate_throttle):
    """rps 参数变化 → 重建桶（配置热更新场景）。"""
    sleeps = _isolate_throttle
    cli = _mock_client(lambda req: _ok_response())
    rules_a = {"timeout_s": 10, "throttle": {"enabled": True, "rps": 10.0, "burst": 1}}
    provider.call_openai_compatible("p", _NODE, rules_a, client=cli)
    # rps=10 → 第二次等待 0.1s
    provider.call_openai_compatible("p", _NODE, rules_a, client=cli)
    assert len(sleeps) == 1 and sleeps[0] < 0.5
    # 切到 rps=0.5 → 桶重建（满令牌）→ 立即通过
    rules_b = {"timeout_s": 10, "throttle": {"enabled": True, "rps": 0.5, "burst": 1}}
    provider.call_openai_compatible("p", _NODE, rules_b, client=cli)
    assert len(sleeps) == 1  # 未新增等待


def test_reset_throttle_clears_state(_isolate_throttle):
    """reset_throttle 后令牌状态清零（新桶满令牌，无需等待）。"""
    sleeps = _isolate_throttle
    rules = {"timeout_s": 10, "throttle": {"enabled": True, "rps": 0.5, "burst": 1}}
    cli = _mock_client(lambda req: _ok_response())
    provider.call_openai_compatible("p", _NODE, rules, client=cli)
    provider.call_openai_compatible("p", _NODE, rules, client=cli)
    assert len(sleeps) == 1
    provider.reset_throttle()
    sleeps.clear()
    provider.call_openai_compatible("p", _NODE, rules, client=cli)  # 新桶满令牌
    assert sleeps == []
