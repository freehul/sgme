# -*- coding: utf-8 -*-
"""T-119：check_llm_available 30s TTL 缓存测试。

背景（B123 NAS 部署实录）：healthcheck 内层 urlopen timeout=3s < LLM 探测实测
5.5s（agnes /models），同步探测必然超时误判 unhealthy。治本 = 探测结果缓存
（stale-while-revalidate）：TTL 内毫秒级返回；过期返回旧值并后台刷新；
client 注入（测试形态）绕过缓存，保证既有 mock 测试零污染。
"""
import time

import httpx
import pytest

from sgme.engine import health as health_mod
from sgme import config as sgme_config
from sgme.llm import provider as llm_provider


# ---------- fixtures ----------

@pytest.fixture(autouse=True)
def _reset_llm_cache():
    """每测试清空模块级缓存，防跨测试串扰。"""
    health_mod.reset_llm_cache()
    yield
    health_mod.reset_llm_cache()


@pytest.fixture
def cfg():
    return sgme_config.load_config()


class _Counter:
    """探测计数器：handler 按 call_no 返回可变状态。"""

    def __init__(self, results):
        # results: list[httpx.Response | Exception]，逐次消费，耗尽后重复最后一个
        self.results = results
        self.calls = 0

    def handler(self, req):
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        r = self.results[idx]
        if isinstance(r, Exception):
            raise r
        return r


def _patch_make_client(monkeypatch, counter: _Counter) -> None:
    """生产形态探测走 make_client——替换为每次新建的 MockTransport 计数客户端。

    注意：必须是工厂（每次新建），不能返回共享单例——_probe_llm 对自建 client
    用后即关（finally close），共享单例首次探测后被 close，后续探测全抛
    RuntimeError 被 except 吞掉，计数不再增长（假象：缓存永远命中）。
    """
    def _factory(timeout_s=5.0):
        return httpx.Client(transport=httpx.MockTransport(counter.handler), trust_env=False)

    monkeypatch.setattr(llm_provider, "make_client", _factory)


# ---------- 用例 ----------

def test_first_call_probes_and_caches(cfg, monkeypatch):
    """首次调用同步探测并写缓存；TTL 内二次调用不再探测。"""
    counter = _Counter([httpx.Response(200, json={"data": []})])
    _patch_make_client(monkeypatch, counter)

    r1 = health_mod.check_llm_available(cfg, ttl=30.0)
    assert r1["available"] is True
    assert counter.calls == 1

    r2 = health_mod.check_llm_available(cfg, ttl=30.0)
    assert r2 == r1
    assert counter.calls == 1  # 缓存命中，零新探测


def test_expired_returns_stale_then_refreshes(cfg, monkeypatch):
    """过期后先返回旧值（stale），后台刷新完成后新值生效。"""
    counter = _Counter([
        httpx.Response(200, json={"data": []}),   # 首次：可用
        httpx.Response(500, json={}),             # 刷新：不可用
    ])
    _patch_make_client(monkeypatch, counter)

    r1 = health_mod.check_llm_available(cfg, ttl=0.05)
    assert r1["available"] is True

    time.sleep(0.08)  # 过期

    r2 = health_mod.check_llm_available(cfg, ttl=0.05)
    assert r2["available"] is True  # stale 旧值，调用方零等待

    # 轮询等待后台刷新落缓存（dream 测试范式：轮询非固定 sleep）
    deadline = time.monotonic() + 3.0
    while counter.calls < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert counter.calls == 2

    # 注意：calls==2 只代表 handler 已返回，线程可能尚未写缓存——
    # 轮询等待新值落缓存（过期路径反复返回 stale 并靠刷新落定），勿读瞬时状态
    r3 = health_mod.check_llm_available(cfg, ttl=0.05)
    while r3["available"] and time.monotonic() < deadline:
        time.sleep(0.02)
        r3 = health_mod.check_llm_available(cfg, ttl=0.05)
    assert r3["available"] is False  # 刷新后的新值
    assert r3["error"] == "HTTP 500"


def test_client_injection_bypasses_cache(cfg):
    """client 注入（测试形态）绕过缓存：每次调用都真探测。"""
    counter = _Counter([httpx.Response(200, json={"data": []})])
    cli = httpx.Client(transport=httpx.MockTransport(counter.handler), trust_env=False)
    try:
        r1 = health_mod.check_llm_available(cfg, client=cli)
        r2 = health_mod.check_llm_available(cfg, client=cli)
        assert r1 == r2
        assert counter.calls == 2
    finally:
        cli.close()


def test_head_change_forces_reprobe(cfg, monkeypatch):
    """首链 provider/model 变化（配置热更新）立即失效缓存重新探测。"""
    counter = _Counter([httpx.Response(200, json={"data": []})])
    _patch_make_client(monkeypatch, counter)

    health_mod.check_llm_available(cfg, ttl=30.0)
    assert counter.calls == 1

    # load_config 返回进程级共享 dict，必须深拷贝构造「另一份配置」
    import copy
    cfg2 = copy.deepcopy(cfg)
    cfg2["llm"]["chains"]["refinement"][0]["model"] = "changed-model"
    health_mod.check_llm_available(cfg2, ttl=30.0)
    assert counter.calls == 2  # head 变化 → 强制重新探测


def test_reset_llm_cache(cfg, monkeypatch):
    """reset 后强制重新探测。"""
    counter = _Counter([httpx.Response(200, json={"data": []})])
    _patch_make_client(monkeypatch, counter)

    health_mod.check_llm_available(cfg, ttl=30.0)
    health_mod.reset_llm_cache()
    health_mod.check_llm_available(cfg, ttl=30.0)
    assert counter.calls == 2


def test_rule_chain_not_cached(cfg):
    """rule 兜底链快速分支不写缓存（零成本且需即时反映配置）。"""
    cfg_rule = {"llm": {"chains": {"refinement": [{"provider": "rule", "rule": "drop_batch"}]}}}
    r1 = health_mod.check_llm_available(cfg_rule)
    r2 = health_mod.check_llm_available(cfg_rule)
    assert r1["available"] is False
    assert r2["available"] is False
    # 内部无缓存写（无探测发生，缓存仍为空）
    assert health_mod._llm_cache is None
