"""llm/chain.py：降级链入口。

- load_config：读取 config/llm.yaml
- validate_models：白名单校验（deny_prefixes + deny_exact），命中抛 ValueError 拒绝启动
- batch_budget：上下文预算 = context_window - reserved_output - prompt_overhead × context_window
- call_with_fallback：链式降级 + 重试（fallback_on 触发，指数退避），全挂 → rule drop_batch
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from sgme import config as sgme_config
from sgme.llm import provider

logger = logging.getLogger("sgme.llm.chain")


# ---------- 配置加载 ----------

def load_config(path: str | None = None) -> dict:
    """读取 llm.yaml（包装 sgme.config.load_llm_config）。"""
    return sgme_config.load_llm_config(path)


# ---------- 白名单校验 ----------

def validate_models(cfg: dict) -> None:
    """校验链中所有模型是否在白名单内。

    - deny_prefixes：模型名以这些前缀开头 → 拒绝
    - deny_exact：模型名完全匹配 → 拒绝
    - 命中任一 → 抛 ValueError（拒绝启动）
    """
    rules = cfg.get("rules", {})
    am = rules.get("allowed_models", {})
    deny_prefixes = am.get("deny_prefixes", [])
    deny_exact = set(am.get("deny_exact", []))

    for chain_name, chain in cfg.get("chains", {}).items():
        for node in chain:
            # rule 节点无 model 字段，跳过
            model = node.get("model")
            if not model:
                continue
            if model in deny_exact:
                raise ValueError(
                    f"模型 {model!r} 命中 deny_exact 黑名单（链 {chain_name}），拒绝加载"
                )
            for prefix in deny_prefixes:
                if prefix in model:
                    raise ValueError(
                        f"模型 {model!r} 命中 deny_prefixes 前缀 {prefix!r}（链 {chain_name}），拒绝加载"
                    )


# ---------- 上下文预算 ----------

def batch_budget(provider_cfg: dict, rules: dict | None = None) -> int:
    """计算单批上下文预算。

    batch_budget = context_window - reserved_output - prompt_overhead × context_window
    """
    ctx_window = provider_cfg.get("context_window")
    if not ctx_window:
        raise ValueError("provider 配置缺 context_window")
    # rules 可来自全局 cfg["rules"]，或显式传入
    if rules is None:
        rules = {"context": {"reserved_output": 4096, "prompt_overhead": 0.08}}
    ctx = rules.get("context", {})
    reserved_output = ctx.get("reserved_output", 4096)
    overhead = ctx.get("prompt_overhead", 0.08)
    budget = int(ctx_window - reserved_output - overhead * ctx_window)
    return max(budget, 0)


# ---------- 链式降级 ----------

def _should_fallback(error: provider.LLMError, fallback_on: list[str]) -> bool:
    """错误是否触发降级。"""
    return error.error_type in fallback_on


def _backoff_delay(rules: dict, attempt: int, retry_after: float | None = None) -> float:
    """指数退避延迟（秒，ST-23⑥）。

    delay = min(base_s × 2^(attempt-1), max_s) + uniform(0, jitter_s)
    429 响应带 Retry-After 头时取 max(delay, retry_after)。
    默认 base 1.0 / max 8.0 / 无抖动（与 llm.yaml rules.backoff 对应）。
    """
    b = rules.get("backoff") or {}
    base = float(b.get("base_s", 1.0))
    cap = float(b.get("max_s", 8.0))
    jitter = float(b.get("jitter_s", 0.0))
    delay = min(base * (2 ** (attempt - 1)), cap)
    if jitter > 0:
        delay += random.uniform(0.0, jitter)
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return delay


def call_with_fallback(
    cfg: dict,
    prompt: str,
    chain_name: str = "refinement",
    client: httpx.Client | None = None,
) -> tuple[str, str, dict]:
    """链式降级调用 LLM。

    - 按链顺序尝试每个 provider
    - 每级重试 max_retries 次（仅对 fallback_on 中错误重试后降级；非 fallback 错误直接抛）
    - 重试间指数退避（rules.backoff，429 优先尊重 Retry-After 头）
    - 全链失败 → rule drop_batch（抛 LLMUnavailable）
    - 返回 (response_text, provider_name, usage)（v0.5：usage 为 token 用量 dict）
    """
    chains = cfg.get("chains", {})
    if chain_name not in chains:
        raise ValueError(f"未知链名: {chain_name}")
    chain = chains[chain_name]
    rules = cfg.get("rules", {})
    max_retries = rules.get("max_retries", 2)
    fallback_on = rules.get("fallback_on", [])

    own_client = client is None
    cli = client or provider.make_client(timeout_s=rules.get("timeout_s", 120))
    last_error: Exception | None = None
    tried_providers: list[str] = []

    try:
        for idx, node in enumerate(chain):
            p_name = node["provider"]
            # 2026-08-22 降级链修复：传 provider_type，让 openai_compat 供应商
            # 免注册可用（此前 providers.yaml 新增供应商若未在 _PROVIDERS 注册，
            # get_provider 抛「未知 provider」→ 兜底级联断裂 → 整批 drop）
            fn = provider.get_provider(p_name, node.get("provider_type"))
            tried_providers.append(p_name)

            # rule 兜底：直接执行（不再重试）
            if p_name == "rule":
                logger.warning(
                    "降级到 rule 兜底 (chain=%s, 已试=%s)", chain_name, tried_providers[:-1]
                )
                # rule 会抛 LLMUnavailable
                fn(prompt, node, rules, cli)

            for attempt in range(1, max_retries + 1):
                try:
                    # v0.5 契约：provider 统一返回 (text, usage) 二元组
                    text, usage = fn(prompt, node, rules, cli)
                    if attempt > 1 or idx > 0:
                        logger.info(
                            "LLM 调用成功 chain=%s provider=%s attempt=%s",
                            chain_name, p_name, attempt,
                        )
                    return text, p_name, usage
                except provider.LLMError as e:
                    last_error = e
                    if e.error_type in fallback_on:
                        if attempt < max_retries:
                            # 指数退避（ST-23⑥）：429 等可重试错误先退避再重试，
                            # 避免立即重试连环撞限流；Retry-After 优先
                            delay = _backoff_delay(
                                rules, attempt, getattr(e, "retry_after", None)
                            )
                            logger.warning(
                                "provider=%s attempt=%s 失败(%s) → %.1fs 后重试(%d/%d): %s",
                                p_name, attempt, e.error_type, delay,
                                attempt + 1, max_retries, e,
                            )
                            time.sleep(delay)
                        else:
                            logger.warning(
                                "provider=%s attempt=%s 失败(%s) → 本级重试耗尽，降级",
                                p_name, attempt, e.error_type,
                            )
                        # 继续下一次重试或降级到下一级
                        continue
                    else:
                        # 非 fallback 错误（如 unknown）直接抛，不降级
                        raise
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "provider=%s attempt=%s 未知异常: %s", p_name, attempt, e
                    )
                    raise

            # 本级重试耗尽，降级到下一级
            logger.warning(
                "provider=%s 重试 %s 次耗尽，降级到下一级", p_name, max_retries
            )

        # 全链失败
        raise provider.LLMUnavailable(
            f"全链降级失败 (chain={chain_name}, tried={tried_providers}, last_error={last_error})"
        )
    finally:
        if own_client:
            cli.close()
