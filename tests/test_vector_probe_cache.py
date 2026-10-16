"""ST-58/T-292：向量连通性探测的进程内 TTL 缓存（P6）。

背景：Docker HEALTHCHECK `interval: 30s` 每 30 秒实调一次 embeddings 端点，
62 天累计 269,104 次请求（≈4,340/天），其中约 2/3 是探针；NAS 纯 CPU 上的本地
Ollama bge-m3 单次约 5s，常年超时 → 健康态 degraded 噪音，且 embed 不可达
53 次触发云端兜底。加 TTL 缓存（120s）后探针流量降约 75%。

设计约束（本文件逐条钉死）：
- 缓存键 = cfg 指纹 `hash((base_url, model, provider, api_key_env))`，
  **配置热更新自动失效**（无需显式清除）；指纹只含密钥**变量名**，不含值（铁律 #10）
- 调用方**显式传 client → 绕过缓存**（「现在就要真实结果」的意图，且让单测确定）
- 返回值是副本，调用方改动不污染缓存
- 结果带 `cached` 字段，便于运维区分「刚探测」与「复用」
"""

from __future__ import annotations

import time

from sgme.operations import health as health_mod
from sgme.operations.health import (
    clear_vector_probe_cache,
    check_vector_model_connectivity,
)


class _FakeResp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _FakeClient:
    """计数用假客户端：只统计 POST 次数，不发网络请求。"""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.posts: list[dict] = []
        self.closed = False

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResp(self.status_code)

    def close(self):
        self.closed = True


def _cfg(base_url: str = "http://127.0.0.1:11434/v1", model: str = "bge-m3"):
    return {
        "search": {
            "vector": {
                "provider": "ollama",
                "model": model,
                "base_url": base_url,
                "api_key_env": "",
            }
        }
    }


def _patch_client(monkeypatch, status_code: int = 200):
    """把 make_client 换成假客户端工厂，返回 (clients 列表)。"""
    clients: list[_FakeClient] = []

    def _factory(timeout_s=None):
        c = _FakeClient(status_code)
        clients.append(c)
        return c

    monkeypatch.setattr(health_mod, "make_client", _factory)
    return clients


def test_cache_hit_avoids_network(monkeypatch):
    """同 cfg 连续调用：只探测一次，第二次走缓存（缓存期内零网络）。"""
    clear_vector_probe_cache()
    clients = _patch_client(monkeypatch)

    r1 = check_vector_model_connectivity(_cfg())
    r2 = check_vector_model_connectivity(_cfg())

    assert r1["available"] is True
    assert r2["available"] is True
    assert len(clients) == 1, f"应只建一次连接（真实探测一次），实际 {len(clients)}"
    assert len(clients[0].posts) == 1
    assert r1["cached"] is False
    assert r2["cached"] is True


def test_fingerprint_change_invalidates(monkeypatch):
    """配置变更（指纹变）→ 自动失效，重新探测（无需显式清除）。"""
    clear_vector_probe_cache()
    clients = _patch_client(monkeypatch)

    check_vector_model_connectivity(_cfg(model="bge-m3"))
    check_vector_model_connectivity(_cfg(model="other-model"))

    assert len(clients) == 2, "cfg 变化后应重新探测"


def test_base_url_change_invalidates(monkeypatch):
    clear_vector_probe_cache()
    clients = _patch_client(monkeypatch)

    check_vector_model_connectivity(_cfg(base_url="http://127.0.0.1:11434/v1"))
    check_vector_model_connectivity(_cfg(base_url="http://127.0.0.1:28080/v1"))

    assert len(clients) == 2


def test_expired_cache_reprobes(monkeypatch):
    """超过 TTL → 过期，重新探测（且过期项被清掉，字典不无界增长）。"""
    clear_vector_probe_cache()
    monkeypatch.setattr(health_mod, "_VECTOR_PROBE_TTL_S", 0.05)
    clients = _patch_client(monkeypatch)

    check_vector_model_connectivity(_cfg())
    time.sleep(0.12)
    check_vector_model_connectivity(_cfg())

    assert len(clients) == 2, "TTL 过期后应重新探测"
    assert len(health_mod._VECTOR_PROBE_CACHE) == 1, "过期项应被清理，缓存不无界增长"


def test_explicit_client_bypasses_cache(monkeypatch):
    """显式传 client（测试/手动核验场景）→ 绕过缓存，每次实探。"""
    clear_vector_probe_cache()
    _patch_client(monkeypatch)  # 防止真建连接

    cli = _FakeClient()
    r1 = check_vector_model_connectivity(_cfg(), client=cli)
    r2 = check_vector_model_connectivity(_cfg(), client=cli)

    assert len(cli.posts) == 2, "显式 client 应每次实探，不读缓存"
    assert r1["cached"] is False and r2["cached"] is False
    assert len(health_mod._VECTOR_PROBE_CACHE) == 0, "显式 client 的探测不写缓存"


def test_cached_result_is_copy(monkeypatch):
    """返回值是副本：调用方改动不得污染缓存。"""
    clear_vector_probe_cache()
    _patch_client(monkeypatch)

    r1 = check_vector_model_connectivity(_cfg())
    r1["available"] = False  # 篡改返回值
    r1["latency_ms"] = 99999

    r2 = check_vector_model_connectivity(_cfg())
    assert r2["available"] is True, "缓存被调用方改动污染了"
    assert r2["latency_ms"] == r1.get("latency_ms") or r2["latency_ms"] != 99999


def test_clear_cache(monkeypatch):
    """clear_vector_probe_cache 可显式清空（配置热更新/测试隔离）。"""
    clear_vector_probe_cache()
    clients = _patch_client(monkeypatch)

    check_vector_model_connectivity(_cfg())
    clear_vector_probe_cache()
    check_vector_model_connectivity(_cfg())

    assert len(clients) == 2


def test_failure_result_also_cached(monkeypatch):
    """失败结果同样缓存（避免故障期把探针打得更凶）。"""
    clear_vector_probe_cache()
    clients = _patch_client(monkeypatch, status_code=503)

    r1 = check_vector_model_connectivity(_cfg())
    r2 = check_vector_model_connectivity(_cfg())

    assert r1["available"] is False and r1["error"] == "HTTP 503"
    assert r2["available"] is False
    assert len(clients) == 1, "失败结果也应缓存，避免故障期放大探测"
