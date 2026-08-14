"""T2 测试：LLM 降级链。

- validate_models 白名单拒绝
- batch_budget 公式数值断言（64K → 约 55K）
- mock httpx 验证降级顺序（timeout/5xx/auth_error/rate_limit → 下一级）
- 429 限流：指数退避重试（ST-23⑥，1s/2s/4s，Retry-After 覆盖）
- 全链失败 → rule drop_batch（LLMUnavailable）
"""

from __future__ import annotations

import json
import time
from copy import deepcopy

import httpx
import pytest

from sgme.llm import chain, provider


# ---------- fixtures ----------

@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch):
    """LLM 测试公共夹具：真实 sleep 替换为记录器（退避/节流不真实等待）。

    - 需要断言退避序列的测试通过返回值拿到 sleeps 记录（time.sleep 调用序列）
    - provider 模块级节流器每测重置，防令牌状态跨测泄漏
    """
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    provider.reset_throttle()
    yield sleeps
    provider.reset_throttle()

@pytest.fixture
def base_cfg():
    """基础 LLM 配置（从真实 llm.yaml 加载）。"""
    return chain.load_config()


@pytest.fixture
def two_llm_cfg(base_cfg):
    """双 LLM 降级链配置（测试降级机制用）。

    真实链是 deepseek → rule（lm-studio 已从 providers.yaml 移除），但降级机制
    测试需要"首链失败 → 降级到次级 LLM 成功"的语义。provider.py 的 _PROVIDERS
    仍注册 lm-studio 函数（代码层注册，与配置文件独立），测试用 mock client
    不发真实请求，故可借用 lm-studio 名作次级 LLM 占位。
    """
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"] = [
        {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "base_url": "http://mock-primary:9999/v1",
            "context_window": 65536,
            "max_tokens": 16384,
        },
        {
            "provider": "lm-studio",
            "model": "qwen/qwen3.5-9b",
            "base_url": "http://mock-fallback:9998/v1",
            "context_window": 65536,
            "max_tokens": 16384,
        },
        {"provider": "rule", "rule": "drop_batch"},
    ]
    return cfg


def _head_url(base_cfg: dict) -> str:
    """降级链首链 base_url（测试与真实配置解耦，不再假设 lm-studio 在首位）。"""
    return base_cfg["chains"]["refinement"][0]["base_url"].rstrip("/")


def _head_provider(base_cfg: dict) -> str:
    """降级链首链 provider 名。"""
    return base_cfg["chains"]["refinement"][0]["provider"]


def _fallback_provider(base_cfg: dict) -> str:
    """降级链次级 provider 名（首链失败后应落到这一级）。"""
    return base_cfg["chains"]["refinement"][1]["provider"]


@pytest.fixture
def good_cfg(base_cfg):
    """合法配置（白名单通过）。"""
    return base_cfg


def _make_node(model: str = "qwythos-9b-claude-mythos-5-1m", **kw) -> dict:
    node = {
        "provider": "lm-studio", "model": model,
        "base_url": "http://127.0.0.1:1014/v1", "context_window": 65536,
    }
    node.update(kw)
    return node


# ---------- 白名单校验 ----------

def test_validate_models_rejects_deny_exact(base_cfg):
    """配置含 gemma-4-12b-qat → ValueError。"""
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"][0]["model"] = "gemma-4-12b-qat"
    with pytest.raises(ValueError, match="deny_exact"):
        chain.validate_models(cfg)


def test_validate_models_rejects_deny_prefix_pro(base_cfg):
    """模型名以 pro 开头 → ValueError。"""
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"][0]["model"] = "pro-deepseek-chat"
    with pytest.raises(ValueError, match="deny_prefixes"):
        chain.validate_models(cfg)


def test_validate_models_rejects_deny_prefix_contains_pro(base_cfg):
    """模型名中间含 pro（deepseek-pro）→ ValueError（前缀词是子串匹配）。"""
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"][0]["model"] = "deepseek-pro"
    with pytest.raises(ValueError, match="deny_prefixes"):
        chain.validate_models(cfg)


def test_validate_models_rejects_deny_prefix_contains_reasoner(base_cfg):
    """模型名中间含 reasoner（qwen-reasoner）→ ValueError。"""
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"][0]["model"] = "qwen-reasoner"
    with pytest.raises(ValueError, match="deny_prefixes"):
        chain.validate_models(cfg)


def test_validate_models_rejects_deny_prefix_contains_thinking(base_cfg):
    """模型名末尾含 thinking（claude-thinking）→ ValueError。"""
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"][0]["model"] = "claude-thinking"
    with pytest.raises(ValueError, match="deny_prefixes"):
        chain.validate_models(cfg)


def test_validate_models_passes_real_model_name(base_cfg):
    """真实合法模型名（qwythos-9b-claude-mythos-5-1m）不含关键词子串 → 不误伤。"""
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"][0]["model"] = "qwythos-9b-claude-mythos-5-1m"
    chain.validate_models(cfg)


def test_validate_models_rejects_deny_prefix_reasoner(base_cfg):
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"][0]["model"] = "reasoner-v2"
    with pytest.raises(ValueError, match="deny_prefixes"):
        chain.validate_models(cfg)


def test_validate_models_rejects_deny_prefix_thinking(base_cfg):
    cfg = deepcopy(base_cfg)
    cfg["chains"]["refinement"][0]["model"] = "thinking-model"
    with pytest.raises(ValueError, match="deny_prefixes"):
        chain.validate_models(cfg)


def test_validate_models_passes_good_config(good_cfg):
    """合法配置不抛。"""
    chain.validate_models(good_cfg)


def test_validate_models_skips_rule_node(base_cfg):
    """rule 节点无 model 字段，跳过。"""
    chain.validate_models(base_cfg)  # 第三级是 rule，无 model


# ---------- batch_budget ----------

def test_batch_budget_64k_returns_about_55k(base_cfg):
    """64K 窗口 → 约 55K（checklist 断言）。

    公式：int(65536 - 4096 - 0.08×65536) = int(56197.12) = 56197 ≈ 55K
    """
    node = {"context_window": 65536}
    rules = base_cfg["rules"]
    b = chain.batch_budget(node, rules)
    # 约 55K（允许 50K-58K 范围）
    assert 50000 <= b <= 58000, f"budget={b} 不在 55K 附近"
    # 精确值断言（与实现一致：整体表达式先算再 int）
    assert b == int(65536 - 4096 - 0.08 * 65536)


def test_batch_budget_131k_deepseek(base_cfg):
    """deepseek 131K 窗口预算。"""
    node = {"context_window": 131072}
    b = chain.batch_budget(node, base_cfg["rules"])
    expected = int(131072 - 4096 - 0.08 * 131072)
    assert b == expected


def test_batch_budget_missing_context_window_raises():
    with pytest.raises(ValueError, match="context_window"):
        chain.batch_budget({"model": "x"})


# ---------- call_with_fallback 降级链（mock httpx） ----------

def _mock_client(handler) -> httpx.Client:
    """构造带 MockTransport 的 httpx 客户端（trust_env=False）。"""
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _ok_response(text: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": text}}]},
    )


def test_call_with_fallback_first_provider_success(base_cfg):
    """主 provider 成功 → 直接返回。"""
    def handler(req):
        return _ok_response("hello")

    cli = _mock_client(handler)
    text, p, _usage = chain.call_with_fallback(base_cfg, "prompt", client=cli)
    assert text == "hello"
    assert p == _head_provider(base_cfg)


def test_call_with_fallback_timeout_falls_to_deepseek(two_llm_cfg):
    """主 provider 超时 → 降级 deepseek 成功。"""
    call_count = {"lm": 0, "ds": 0}

    def handler(req):
        # 主 provider 重试 2 次都超时（通过抛 httpx.TimeoutException）
        # 用 URL 区分：首链（当前为 deepseek）与次级链
        if _head_url(two_llm_cfg) in str(req.url):
            call_count["lm"] += 1
            raise httpx.TimeoutException("simulated timeout")
        call_count["ds"] += 1
        return _ok_response("from deepseek")

    cli = _mock_client(handler)
    text, p, _usage = chain.call_with_fallback(two_llm_cfg, "prompt", client=cli)
    assert text == "from deepseek"
    assert p == _fallback_provider(two_llm_cfg)
    assert call_count["lm"] == 2  # 重试 2 次
    assert call_count["ds"] == 1


def test_call_with_fallback_5xx_falls_to_deepseek(two_llm_cfg):
    """主 provider 5xx → 降级 deepseek。"""
    def handler(req):
        if _head_url(two_llm_cfg) in str(req.url):
            return httpx.Response(503, text="service unavailable")
        return _ok_response("deepseek ok")

    cli = _mock_client(handler)
    text, p, _usage = chain.call_with_fallback(two_llm_cfg, "prompt", client=cli)
    assert p == _fallback_provider(two_llm_cfg)


def test_call_with_fallback_auth_error_falls_to_deepseek(two_llm_cfg):
    """主 provider 401 → 降级 deepseek。"""
    def handler(req):
        if _head_url(two_llm_cfg) in str(req.url):
            return httpx.Response(401, text="unauthorized")
        return _ok_response("ok")

    cli = _mock_client(handler)
    text, p, _usage = chain.call_with_fallback(two_llm_cfg, "prompt", client=cli)
    assert p == _fallback_provider(two_llm_cfg)


def test_call_with_fallback_connection_error_drops_to_rule(base_cfg):
    """全链失败 → rule drop_batch（LLMUnavailable）。"""
    def handler(req):
        # 所有 provider 都连接错误
        raise httpx.ConnectError("connection refused")

    cli = _mock_client(handler)
    with pytest.raises(provider.LLMUnavailable):
        chain.call_with_fallback(base_cfg, "prompt", client=cli)


def test_call_with_fallback_all_fail_raises_unavailable(base_cfg):
    """主+备都失败（超时+超时）→ rule drop_batch。"""
    def handler(req):
        raise httpx.TimeoutException("always timeout")

    cli = _mock_client(handler)
    with pytest.raises(provider.LLMUnavailable):
        chain.call_with_fallback(base_cfg, "prompt", client=cli)


def test_call_with_fallback_context_overflow_falls(two_llm_cfg):
    """context_overflow（400 + context length）→ 降级。"""
    def handler(req):
        if _head_url(two_llm_cfg) in str(req.url):
            return httpx.Response(400, text="This model's maximum context length is exceeded")
        return _ok_response("from deepseek")

    cli = _mock_client(handler)
    text, p, _usage = chain.call_with_fallback(two_llm_cfg, "prompt", client=cli)
    assert p == _fallback_provider(two_llm_cfg)


def test_call_with_fallback_unknown_error_not_fallback(base_cfg):
    """非 fallback_on 错误（如 422 unknown）→ 直接抛，不降级。"""
    def handler(req):
        if _head_url(base_cfg) in str(req.url):
            return httpx.Response(422, text="unprocessable")
        return _ok_response("should not reach")

    cli = _mock_client(handler)
    with pytest.raises(provider.LLMError) as exc_info:
        chain.call_with_fallback(base_cfg, "prompt", client=cli)
    # error_type 应是 unknown（不在 fallback_on）
    assert exc_info.value.error_type == "unknown"


def test_call_with_fallback_unknown_chain_raises(base_cfg):
    with pytest.raises(ValueError, match="未知链名"):
        chain.call_with_fallback(base_cfg, "prompt", chain_name="nope")


def test_call_with_fallback_no_client_creates_own(base_cfg, monkeypatch):
    """不传 client → 内部创建（trust_env=False 铁律）。"""
    created = {}
    orig_make = provider.make_client

    def spy_make(timeout_s=120.0):
        c = orig_make(timeout_s)
        created["trust_env"] = c._trust_env if hasattr(c, "_trust_env") else None
        return c

    monkeypatch.setattr(provider, "make_client", spy_make)

    def handler(req):
        return _ok_response("ok")

    # 注入 mock transport 到 spy 创建的 client
    orig_init = httpx.Client.__init__

    def patched_init(self, *args, **kw):
        kw.setdefault("transport", httpx.MockTransport(handler))
        kw["trust_env"] = False
        orig_init(self, *args, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    text, p, _usage = chain.call_with_fallback(base_cfg, "prompt")
    assert text == "ok"
    assert created.get("trust_env") is False


# ---------- provider 单元测试 ----------

def test_get_provider_unknown_raises():
    with pytest.raises(ValueError, match="未知 provider"):
        provider.get_provider("nonexistent")


def test_rule_drop_batch_raises_unavailable():
    with pytest.raises(provider.LLMUnavailable):
        provider.call_rule_drop_batch("x", {"rule": "drop_batch"}, {})


def test_make_client_trust_env_false():
    """httpx 客户端必须 trust_env=False（铁律）。"""
    c = provider.make_client()
    try:
        assert c._trust_env is False
    finally:
        c.close()


# ---------- 429 限流（ST-23⑥：rate_limit 入 fallback_on + 指数退避） ----------

def test_llm_config_fallback_on_includes_rate_limit(base_cfg):
    """llm.yaml fallback_on 必须含 rate_limit（ST-23⑥ 验收）。"""
    assert "rate_limit" in base_cfg["rules"]["fallback_on"]


def test_classify_429_as_rate_limit():
    """429 → error_type=rate_limit（此前落入 unknown 直接抛，不重试不降级）。"""
    resp = httpx.Response(429, text="rate limit exceeded")
    err = provider._classify_http_error(resp)
    assert err.error_type == "rate_limit"


def test_classify_429_parses_retry_after():
    """429 带 Retry-After 头（秒）→ retry_after 透传。"""
    resp = httpx.Response(429, headers={"retry-after": "5"}, text="slow down")
    err = provider._classify_http_error(resp)
    assert err.error_type == "rate_limit"
    assert err.retry_after == 5.0


def test_classify_429_invalid_retry_after_ignored():
    """Retry-After 非数字（HTTP-date 格式）→ None，不抛。"""
    resp = httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, text="x")
    err = provider._classify_http_error(resp)
    assert err.error_type == "rate_limit"
    assert err.retry_after is None


def test_backoff_delay_sequence_and_cap():
    """退避公式：min(base × 2^(n-1), max) + jitter；Retry-After 覆盖取 max。"""
    rules = {"backoff": {"base_s": 1.0, "max_s": 8.0, "jitter_s": 0.0}}
    assert chain._backoff_delay(rules, 1) == 1.0
    assert chain._backoff_delay(rules, 2) == 2.0
    assert chain._backoff_delay(rules, 3) == 4.0
    assert chain._backoff_delay(rules, 4) == 8.0  # 封顶
    assert chain._backoff_delay(rules, 5) == 8.0
    # Retry-After 覆盖退避
    assert chain._backoff_delay(rules, 1, retry_after=5.0) == 5.0
    assert chain._backoff_delay(rules, 5, retry_after=3.0) == 8.0  # max(8, 3)


def test_backoff_delay_defaults_without_config():
    """无 backoff 配置 → 默认 base 1.0 / max 8.0 / 无抖动。"""
    assert chain._backoff_delay({}, 2) == 2.0


def test_call_with_fallback_rate_limit_retries_then_falls(two_llm_cfg, _fast_and_isolated):
    """429 → 指数退避重试（1s/2s）→ 仍失败才降级下一级（不直接抛）。"""
    sleeps = _fast_and_isolated

    def handler(req):
        if _head_url(two_llm_cfg) in str(req.url):
            return httpx.Response(429, text="rate limited")
        return _ok_response("from fallback")

    cfg = deepcopy(two_llm_cfg)
    cfg["rules"]["max_retries"] = 4  # 4 次尝试 → 3 个重试间隙，展示完整 1s/2s/4s 序列
    cfg["rules"]["backoff"] = {"base_s": 1.0, "max_s": 8.0, "jitter_s": 0.0}
    cfg["rules"]["throttle"] = {"enabled": False}  # 退避测试隔离节流器（节流另测）
    cli = _mock_client(handler)
    text, p, _usage = chain.call_with_fallback(cfg, "prompt", client=cli)
    assert p == _fallback_provider(two_llm_cfg)
    assert sleeps == [1.0, 2.0, 4.0]  # 指数退避序列（ST-23⑥ 验收：1s/2s/4s）


def test_call_with_fallback_rate_limit_all_fail_raises_unavailable(two_llm_cfg, _fast_and_isolated):
    """全链 429 且退避重试耗尽 → LLMUnavailable（rule drop_batch 兜底，不直接抛）。"""
    sleeps = _fast_and_isolated

    def handler(req):
        return httpx.Response(429, text="rate limited")

    cfg = deepcopy(two_llm_cfg)
    cfg["rules"]["backoff"] = {"base_s": 1.0, "max_s": 8.0, "jitter_s": 0.0}
    cfg["rules"]["throttle"] = {"enabled": False}
    cli = _mock_client(handler)
    with pytest.raises(provider.LLMUnavailable):
        chain.call_with_fallback(cfg, "prompt", client=cli)
    # 默认 max_retries=2 → 每 provider 1 个重试间隙（1.0s），两个 LLM provider 共 2 个间隙
    assert sleeps == [1.0, 1.0]


def test_call_with_fallback_rate_limit_honors_retry_after(two_llm_cfg, _fast_and_isolated):
    """429 带 Retry-After 头 → 退避取 max(指数退避, Retry-After)。"""
    sleeps = _fast_and_isolated

    def handler(req):
        if _head_url(two_llm_cfg) in str(req.url):
            return httpx.Response(429, headers={"retry-after": "5"}, text="slow down")
        return _ok_response("ok")

    cfg = deepcopy(two_llm_cfg)
    cfg["rules"]["max_retries"] = 3  # 2 个重试间隙，两次都尊重 Retry-After=5
    cfg["rules"]["backoff"] = {"base_s": 1.0, "max_s": 8.0, "jitter_s": 0.0}
    cfg["rules"]["throttle"] = {"enabled": False}
    cli = _mock_client(handler)
    text, p, _usage = chain.call_with_fallback(cfg, "prompt", client=cli)
    assert p == _fallback_provider(two_llm_cfg)
    assert sleeps == [5.0, 5.0]
