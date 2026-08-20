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
