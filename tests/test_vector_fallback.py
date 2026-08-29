"""vector.embed 多 provider 降级链测试（TDD：先失败，后实现）。

策略（2026-08-20 生产定案）：SGME 向量 embedding 本地优先、云端免费降级。
主 provider = 本地 Ollama（bge-m3 1024 维）；fallback = 硅基流动 siliconflow 云端（同 1024 维）。
本测试先验证现状（主失败不尝试 fallback → 返回 None），实现后验证降级链正确。
"""
from __future__ import annotations

import httpx
import pytest

from sgme.data.search import vector as vector_mod


# ---------- 工具 ----------

def _embed_client(*responses: httpx.Response) -> httpx.Client:
    """构造 mock httpx 客户端，按顺序返回给定响应（超出则复用最后一个）。"""
    state = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        i = state["i"]
        state["i"] = i + 1
        return responses[min(i, len(responses) - 1)]

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _ok_embedding(vec: list[float]) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"embedding": list(vec)}]})


def _cfg_with_fallbacks(primary: dict, fallbacks: list[dict]) -> dict:
    """构造含 fallbacks 的 search.vector 配置。"""
    return {"search": {"vector": {"model": primary.get("model", "bge-m3"),
                                  "base_url": primary.get("base_url", ""),
                                  "api_key_env": primary.get("api_key_env", ""),
                                  "fallbacks": fallbacks}}}


# ---------- 现状：主失败不尝试 fallback ----------

def test_embed_primary_fail_returns_none_without_fallback():
    """现状：主 provider 不可达 → embed() 直接返回 None，不尝试 fallback。

    这是 TDD 的失败测试——实现「多 provider 降级链」后，本测试应改为验证
    fallback 被尝试。
    """
    # 主 provider：连接失败（socket error 模拟）
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    cfg = _cfg_with_fallbacks(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "bge-m3"},
        [{"base_url": "https://api.siliconflow.cn/v1", "model": "BAAI/bge-m3",
          "api_key_env": "SILICONFLOW_API_KEY"}],
    )

    got = vector_mod.embed("hello", cfg, client=cli)
    assert got is None, "现状：主 provider 失败应返回 None（未实现 fallback）"


# ---------- 目标行为（实现后应通过）----------

def test_embed_primary_fail_then_fallback_success():
    """主 provider 失败 → 自动尝试 fallback → 返回 fallback 向量。

    2026-08-20 实现：多 provider 降级链（本地优先、云端免费降级）。
    """
    cli = _embed_client(
        httpx.Response(503, text="service unavailable"),  # 主 provider 失败
        _ok_embedding([0.1, 0.2, 0.3]),                     # fallback 成功
    )
    cfg = _cfg_with_fallbacks(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "bge-m3"},
        [{"base_url": "https://api.siliconflow.cn/v1", "model": "BAAI/bge-m3"}],
    )

    got = vector_mod.embed("hello", cfg, client=cli)
    assert got == [0.1, 0.2, 0.3]


def test_embed_primary_success_skips_fallback():
    """主 provider 成功 → 不调用 fallback（零多余请求，现状已满足）。"""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return _ok_embedding([0.5, 0.5])

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    cfg = _cfg_with_fallbacks(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "bge-m3"},
        [{"base_url": "https://api.siliconflow.cn/v1", "model": "BAAI/bge-m3"}],
    )

    got = vector_mod.embed("hello", cfg, client=cli)
    assert got == [0.5, 0.5]
    assert len(calls) == 1, "主 provider 成功时不应调用 fallback"


def test_embed_all_fail_returns_none():
    """主 + 全部 fallback 都失败 → 返回 None（降级纯 BM25，现状已满足）。"""
    cli = _embed_client(
        httpx.Response(500, text="boom"),
        httpx.Response(502, text="bad gateway"),
    )
    cfg = _cfg_with_fallbacks(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "bge-m3"},
        [{"base_url": "https://api.siliconflow.cn/v1", "model": "BAAI/bge-m3"}],
    )

    got = vector_mod.embed("hello", cfg, client=cli)
    assert got is None


# ---------- T-117：链首回退 vector_capable 门禁（2026-08-29）----------

def _cfg_no_vector_base_url(chain_head: dict) -> dict:
    """search.vector 无 base_url（触发链首回退）+ 指定链首节点。"""
    return {
        "search": {"vector": {"model": "bge-m3"}},
        "llm": {"chains": {"refinement": [chain_head]}},
    }


def test_embed_skips_head_without_vector_capable(monkeypatch):
    """链首在注册表且 vector_capable=false（agnes，9af882b 后无 embeddings 服务）
    → 不发注定 401/404 的请求，直接返回 None 降级 BM25（T-117）。"""
    import sgme.config as config_mod

    monkeypatch.setattr(config_mod, "load_providers_config", lambda: {
        "agnes": {
            "name": "agnes",
            "base_url": "https://apihub.agnes-ai.cn/v1",
            "vector_capable": False,
        },
    }, raising=False)
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return _ok_embedding([0.1])

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    cfg = _cfg_no_vector_base_url(
        {"provider": "agnes", "base_url": "https://apihub.agnes-ai.cn/v1"}
    )
    got = vector_mod.embed("t117-gate-unique-text", cfg, client=cli)
    assert got is None
    assert calls == [], "vector_capable=false 的链首不应发起任何请求"


def test_embed_fallback_head_without_provider_name_unchanged():
    """链首无 provider 名（旧配置形态/本地链首）→ 保持旧行为照常回退。"""
    cli = _embed_client(_ok_embedding([0.4, 0.4]))
    cfg = _cfg_no_vector_base_url({"base_url": "http://127.0.0.1:1014/v1"})
    got = vector_mod.embed("t117-legacy-unique-text", cfg, client=cli)
    assert got == [0.4, 0.4]


def test_embed_fallback_head_vector_capable_uses_registry_model(monkeypatch):
    """链首在注册表且 vector_capable=true → 照常回退，且模型/密钥对齐注册表字段。"""
    import sgme.config as config_mod

    monkeypatch.setattr(config_mod, "load_providers_config", lambda: {
        "siliconflow": {
            "name": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "default_model": "BAAI/bge-m3",
            "api_key_env": "T117_KEY_ENV",
            "vector_capable": True,
        },
    }, raising=False)
    monkeypatch.setenv("T117_KEY_ENV", "k-registry")

    class RecClient:
        """记录请求的假客户端（vector.embed 消费 post/status_code/json）。"""

        def __init__(self):
            self.calls = []

        def post(self, url, json=None, headers=None):  # noqa: A002
            self.calls.append((url, json, headers))
            return httpx.Response(200, json={"data": [{"embedding": [0.7, 0.7]}]})

        def close(self):
            pass

    cli = RecClient()  # type: ignore[arg-type]  # 测试假客户端，运行时 duck-typing
    # search.vector 不配 model/base_url → 连接字段应全部来自注册表链首对齐
    cfg = {
        "search": {"vector": {}},
        "llm": {"chains": {"refinement": [
            {"provider": "siliconflow", "base_url": "https://api.siliconflow.cn/v1"}
        ]}},
    }
    got = vector_mod.embed("t117-registry-unique-text", cfg, client=cli)
    assert got == [0.7, 0.7]
    url, payload, headers = cli.calls[0]
    assert url == "https://api.siliconflow.cn/v1/embeddings"
    assert payload["model"] == "BAAI/bge-m3", "注册表 default_model 应对齐到请求"
    assert headers.get("Authorization") == "Bearer k-registry"
