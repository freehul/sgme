"""vector.embed base_url 解析测试（PR #6：embedding 独立端点配置）。

背景：embed 原借用 LLM 降级链首批 provider 的 base_url——主模型切 DeepSeek 后
embedding 请求发往 DeepSeek（无 embeddings API，401）。修复后优先
search.vector.base_url，缺省回退 refinement[0]（向后兼容）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgme.data.search import vector  # noqa: E402


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
        self.text = "fake"

    def json(self):
        return self._data


class FakeClient:
    """记录请求 url/payload/headers，返回假 embedding。"""

    def __init__(self, status=200, data=None):
        self.calls = []
        self._status = status
        self._data = data

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return FakeResp(self._status, self._data)

    def close(self):
        pass


def make_cfg(base_url=None):
    cfg = {
        "search": {"vector": {"enabled": True, "model": "text-embedding-nomic-embed-text-v1.5"}},
        "llm": {"chains": {"refinement": [{"base_url": "https://api.deepseek.com/v1"}]}},
    }
    if base_url:
        cfg["search"]["vector"]["base_url"] = base_url
    return cfg


def test_embed_uses_vector_base_url_first():
    """search.vector.base_url 优先（不回退到 LLM 链）。"""
    client = FakeClient()
    vec = vector.embed("测试文本", make_cfg(base_url="http://127.0.0.1:1014/v1"), client=client)
    assert vec == [0.1, 0.2, 0.3]
    url, payload, _ = client.calls[0]
    assert url == "http://127.0.0.1:1014/v1/embeddings"
    assert payload["model"] == "text-embedding-nomic-embed-text-v1.5"


def test_embed_bearer_auth_with_api_key_env(monkeypatch):
    """api_key_env 声明后请求带 Bearer 头（方舟 plan 通道）。"""
    monkeypatch.setenv("VOLC_API_KEY", "ark-test-key-123")
    cfg = make_cfg(base_url="https://ark.cn-beijing.volces.com/api/plan/v3")
    cfg["search"]["vector"]["api_key_env"] = "VOLC_API_KEY"
    cfg["search"]["vector"]["model"] = "doubao-embedding-vision"
    client = FakeClient()
    vector.embed("测试", cfg, client=client)
    url, _, headers = client.calls[0]
    assert url == "https://ark.cn-beijing.volces.com/api/plan/v3/embeddings"
    assert headers.get("Authorization") == "Bearer ark-test-key-123"


def test_embed_no_auth_without_api_key_env():
    """未配置 api_key_env → 不带鉴权头（本地 LM Studio 兼容）。"""
    client = FakeClient()
    vector.embed("测试文本", make_cfg(base_url="http://127.0.0.1:1014/v1"), client=client)
    _, _, headers = client.calls[0]
    assert not headers or "Authorization" not in headers


def test_embed_fallback_to_refinement_chain():
    """无独立配置时回退 LLM 链首批 base_url（向后兼容）。"""
    client = FakeClient()
    vector.embed("测试文本", make_cfg(), client=client)
    url, _, _ = client.calls[0]
    assert url == "https://api.deepseek.com/v1/embeddings"


def test_embed_non_200_returns_none():
    client = FakeClient(status=401)
    assert vector.embed("测试文本", make_cfg(base_url="http://127.0.0.1:1014/v1"), client=client) is None


def test_embed_no_cfg_returns_none_without_exception():
    """缺配置不抛异常，静默返回 None（向量降级 BM25）。"""
    client = FakeClient()
    assert vector.embed("测试文本", {}, client=client) is None
    assert vector.embed("测试文本", {"search": {}}, client=client) is None
