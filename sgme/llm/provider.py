"""llm/provider.py：三种 provider 适配层。

- lm_studio / deepseek：OpenAI 兼容 /chat/completions
- rule：drop_batch（兜底，抛 LLMUnavailable）

铁律：
- httpx 客户端必须 trust_env=False（防 Clash 代理劫持 localhost 请求）
- 密钥只引用环境变量名，不落盘
- 所有异常分类为：timeout / connection_error / auth_error / 5xx / rate_limit /
  context_overflow / unknown
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from sgme import config
from sgme.llm.throttle import TokenBucket

logger = logging.getLogger("sgme.llm.provider")


class LLMError(Exception):
    """LLM 调用错误基类。携带 error_type 用于 fallback_on 判定。"""

    def __init__(
        self,
        message: str,
        error_type: str = "unknown",
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        # 429 响应的 Retry-After 头（秒）；无则 None（ST-23⑥）
        self.retry_after = retry_after


class LLMUnavailable(LLMError):
    """全链失败（rule drop_batch 兜底）。"""

    def __init__(self, message: str = "全链降级失败，drop_batch"):
        super().__init__(message, error_type="drop_batch")


class ContextOverflow(LLMError):
    """输入超当前模型窗口。"""

    def __init__(self, message: str = "context overflow"):
        super().__init__(message, error_type="context_overflow")


# ---------- httpx 客户端工厂 ----------

def make_client(timeout_s: float = 120.0) -> httpx.Client:
    """创建 httpx 客户端：trust_env=False（铁律）。"""
    return httpx.Client(timeout=timeout_s, trust_env=False)


# ---------- OpenAI 兼容调用（lm_studio / deepseek 共用） ----------

# 默认采样参数（Qwythos-9B-v2 官方推荐，empero-ai/Qwythos-9B-v2 模型卡）：
# temperature=0.6, top_p=0.95, top_k=20, repetition_penalty=1.05
# 单 provider 可在 llm.yaml 节点里用 sampling 覆盖
_OPENAI_PAYLOAD_TEMPLATE = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "repetition_penalty": 1.05,
    "max_tokens": 4096,
    "stream": False,
}


def _classify_http_error(resp: httpx.Response) -> LLMError:
    """根据 HTTP 状态码分类错误。"""
    if resp.status_code == 429:
        # 限流（ST-23⑥）：error_type=rate_limit 入 fallback_on →
        # 指数退避重试，不再落入 unknown 直接抛
        return LLMError(
            f"限流 {resp.status_code}: {resp.text[:200]}",
            error_type="rate_limit",
            retry_after=_parse_retry_after(resp),
        )
    if resp.status_code in (401, 403):
        return LLMError(f"鉴权失败 {resp.status_code}", error_type="auth_error")
    if resp.status_code >= 500:
        return LLMError(f"服务端错误 {resp.status_code}", error_type="5xx")
    if resp.status_code == 400:
        # context_overflow 通常返回 400 且 body 含 context length / too long
        body = (resp.text or "").lower()
        if "context" in body and ("length" in body or "long" in body or "overflow" in body):
            return ContextOverflow(f"上下文超长: {resp.text[:200]}")
    return LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}", error_type="unknown")


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """解析 Retry-After 头（秒）。HTTP-date 等非数字格式忽略（返回 None）。"""
    ra = resp.headers.get("retry-after")
    if not ra:
        return None
    try:
        return max(float(ra), 0.0)
    except ValueError:
        return None


# ---------- 调用层节流（ST-23⑥：批量提炼时平滑请求速率，防连环撞限流） ----------

_throttler: TokenBucket | None = None
_throttler_params: tuple[float, float] | None = None


def _acquire_throttle(rules: dict) -> float:
    """按 rules.throttle 配置获取调用令牌（阻塞直到可用）。

    - 未配置或 enabled=false → 0（不等待，行为不变）
    - 参数变化时重建桶（配置热更新场景）
    - 返回等待秒数（供日志/测试断言）
    """
    global _throttler, _throttler_params
    th = rules.get("throttle") or {}
    if not th.get("enabled", False):
        return 0.0
    rps = float(th.get("rps", 0.5))
    burst = float(th.get("burst", 1.0))
    params = (rps, burst)
    if _throttler is None or _throttler_params != params:
        _throttler = TokenBucket(rate=rps, capacity=burst)
        _throttler_params = params
    return _throttler.acquire(1.0)


def reset_throttle() -> None:
    """重置节流器（测试用：清缓存 + 令牌状态归零）。"""
    global _throttler, _throttler_params
    _throttler = None
    _throttler_params = None


def call_openai_compatible(
    prompt: str,
    provider_cfg: dict,
    rules: dict,
    client: httpx.Client | None = None,
) -> tuple[str, dict]:
    """OpenAI 兼容 /chat/completions 调用（lm_studio / deepseek 共用）。

    - provider_cfg：链节配置（provider/model/base_url/api_key_env/context_window）
    - rules：全局规则（timeout_s/max_retries/...）
    - client：可注入 httpx 客户端（测试用 mock）
    - 返回 (text, usage) 二元组（v0.5 起透传 token 用量，供调用方记账）
    - 失败抛 LLMError 子类（携带 error_type）
    """
    base_url = provider_cfg["base_url"].rstrip("/")
    model = provider_cfg["model"]
    api_key_env = provider_cfg.get("api_key_env")
    api_key = os.environ.get(api_key_env) if api_key_env else "not-required"
    timeout_s = rules.get("timeout_s", 120)

    own_client = client is None
    cli = client or make_client(timeout_s=timeout_s)
    try:
        # 采样参数：provider 节点 sampling 覆盖 > 默认模板
        # 注意：sampling 存在时按「显式声明」传参——官方推荐 Disabled 的参数
        # （如 qwen 的 repeat_penalty/min_p）就不传，让 LM Studio 用默认
        sampling = provider_cfg.get("sampling") or {}
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": sampling.get("temperature", _OPENAI_PAYLOAD_TEMPLATE["temperature"]),
            "top_p": sampling.get("top_p", _OPENAI_PAYLOAD_TEMPLATE["top_p"]),
            "top_k": sampling.get("top_k", _OPENAI_PAYLOAD_TEMPLATE["top_k"]),
            "max_tokens": provider_cfg.get("max_tokens", 4096),
            "stream": False,
        }
        # 可选采样参数：仅在 sampling 显式声明时传（官方 Disabled = 不传）
        if sampling:
            for opt_key in ("repetition_penalty", "presence_penalty", "min_p"):
                if opt_key in sampling:
                    payload[opt_key] = sampling[opt_key]
        else:
            # 无 sampling 配置：用默认模板的重复惩罚
            payload["repetition_penalty"] = _OPENAI_PAYLOAD_TEMPLATE["repetition_penalty"]
        # 额外请求体参数（2026-08-06）：如 DeepSeek V4 关思考
        #   {"thinking": {"type": "disabled"}}
        extra = provider_cfg.get("extra_body") or {}
        if extra:
            payload.update(extra)
        headers = {"Authorization": f"Bearer {api_key}"}
        # 调用层节流（ST-23⑥）：批量提炼时平滑请求速率，防连环撞限流
        wait = _acquire_throttle(rules)
        if wait > 0:
            logger.info("节流等待 %.1fs（批量限流平滑）", wait)
        try:
            resp = cli.post(f"{base_url}/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMError(f"超时: {e}", error_type="timeout") from e
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise LLMError(f"连接错误: {e}", error_type="connection_error") from e
        except httpx.HTTPError as e:
            raise LLMError(f"HTTP 错误: {e}", error_type="unknown") from e

        if resp.status_code != 200:
            raise _classify_http_error(resp)

        data = resp.json()
        # OpenAI 兼容返回结构：choices[0].message.content
        # v0.5（2026-08-06）：透传 usage（prompt/completion/total tokens），
        # 供调用方记账（DeepSeek 用量统计，含 cache 明细）
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return text, usage
    finally:
        if own_client:
            cli.close()


def call_lm_studio(prompt: str, provider_cfg: dict, rules: dict, client: httpx.Client | None = None) -> tuple[str, dict]:
    """lm-studio provider。"""
    return call_openai_compatible(prompt, provider_cfg, rules, client)


def call_deepseek(prompt: str, provider_cfg: dict, rules: dict, client: httpx.Client | None = None) -> tuple[str, dict]:
    """deepseek provider。"""
    return call_openai_compatible(prompt, provider_cfg, rules, client)


def call_rule_drop_batch(prompt: str, provider_cfg: dict, rules: dict, client: httpx.Client | None = None) -> tuple[str, dict]:
    """rule 兜底：drop_batch。

    抛 LLMUnavailable，调用方应将该批标记未提炼。
    """
    raise LLMUnavailable()


# ---------- provider 路由 ----------

_PROVIDERS = {
    "lm-studio": call_lm_studio,
    "lm_studio": call_lm_studio,
    "deepseek": call_deepseek,
    "rule": call_rule_drop_batch,
}


def get_provider(name: str):
    """按名取 provider 函数。未知 provider 抛 ValueError。"""
    fn = _PROVIDERS.get(name)
    if fn is None:
        raise ValueError(f"未知 provider: {name}")
    return fn
