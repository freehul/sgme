"""tests/test_vector_connectivity.py：向量模型连通性探测 + 失效信号（T-53 2026-08-18）。

覆盖：
1. check_vector_model_connectivity：200 → available；HTTP 错误 → 不可用；超时 → 不可用；未配置 → 不可用
2. _vector_block：连通失败时发布 anomaly_warn（source=vector）信号
3. 提炼链顺序：agnes 主、siliconflow 备（2026-08-29 B121，zhipu 已移出）
"""
from __future__ import annotations

import copy

import pytest

from sgme import config as sgme_config
from sgme.operations.health import (
    _vector_block,
    check_vector_model_connectivity,
)


@pytest.fixture
def cfg():
    return sgme_config.load_config()


def _vec_cfg(cfg, **kw):
    """构造带 search.vector 的 cfg 副本（测试环境 sgme.yaml 被 conftest 隔离）。"""
    c2 = copy.deepcopy(cfg)
    v = {
        "enabled": True, "provider": "test-prov", "model": "test-model",
        "base_url": "https://example.com/v1", "api_key_env": "TEST_VECTOR_KEY",
    }
    v.update(kw)
    c2.setdefault("search", {})["vector"] = v
    return c2


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _Cli:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def post(self, *args, **kwargs):
        if self._exc:
            raise self._exc
        return self._resp


# ---------- 1. connectivity 探测 ----------

def test_connectivity_ok(cfg, monkeypatch):
    monkeypatch.setenv("TEST_VECTOR_KEY", "k")
    cli = _Cli(_Resp(200))
    r = check_vector_model_connectivity(_vec_cfg(cfg), client=cli)
    assert r["available"] is True
    assert r["model"] == "test-model"
    assert r["latency_ms"] >= 0
    assert r["error"] is None


def test_connectivity_http_error(cfg, monkeypatch):
    monkeypatch.setenv("TEST_VECTOR_KEY", "k")
    cli = _Cli(_Resp(500))
    r = check_vector_model_connectivity(_vec_cfg(cfg), client=cli)
    assert r["available"] is False
    assert "500" in r["error"]


def test_connectivity_timeout(cfg, monkeypatch):
    import httpx
    monkeypatch.setenv("TEST_VECTOR_KEY", "k")
    cli = _Cli(exc=httpx.TimeoutException("timeout"))
    r = check_vector_model_connectivity(_vec_cfg(cfg), client=cli)
    assert r["available"] is False
    assert r["error"]


def test_connectivity_unconfigured(cfg):
    # 测试环境默认 search.vector 无 base_url → 不可用 + 中文提示
    r = check_vector_model_connectivity(cfg)
    assert r["available"] is False
    assert "未配置" in r["error"]


def test_connectivity_missing_key_still_probes(cfg, monkeypatch):
    """api_key_env 缺 key 时不带头但仍探测（服务端可能 401/200 由端点决定）。"""
    monkeypatch.delenv("TEST_VECTOR_KEY", raising=False)
    cli = _Cli(_Resp(200))
    r = check_vector_model_connectivity(_vec_cfg(cfg), client=cli)
    assert r["available"] is True


# ---------- 2. _vector_block 失效信号 ----------

def test_vector_block_failure_publishes_signal(cfg, monkeypatch):
    """连通失败 → 发布 anomaly_warn（source=vector）+ 日志。"""
    import sgme.operations.health as health_mod
    import sgme.signal.engine as signal_engine

    calls: list[dict] = []
    monkeypatch.setattr(
        health_mod, "check_vector_model_connectivity",
        lambda c, client=None: {
            "available": False, "provider": "siliconflow",
            "model": "BAAI/bge-m3", "latency_ms": 5, "error": "boom",
        },
    )
    monkeypatch.setattr(
        signal_engine, "publish",
        lambda **kw: calls.append(kw) or "eid",
    )
    _vector_block(None, cfg)
    assert len(calls) == 1
    assert calls[0]["event_type"] == "anomaly_warn"
    assert calls[0]["source"] == "vector"
    assert calls[0]["payload"]["component"] == "vector_model"
    assert "SILICONFLOW_API_KEY" in calls[0]["payload"]["hint"]


def test_vector_block_ok_no_signal(cfg, monkeypatch):
    """连通正常 → 不发信号。"""
    import sgme.operations.health as health_mod
    import sgme.signal.engine as signal_engine

    calls: list[dict] = []
    monkeypatch.setattr(
        health_mod, "check_vector_model_connectivity",
        lambda c, client=None: {
            "available": True, "provider": "siliconflow",
            "model": "BAAI/bge-m3", "latency_ms": 172, "error": None,
        },
    )
    monkeypatch.setattr(
        signal_engine, "publish",
        lambda **kw: calls.append(kw) or "eid",
    )
    _vector_block(None, cfg)
    assert calls == []


# ---------- 3. 提炼链顺序（主 agnes 备 siliconflow） ----------

def test_chain_agnes_primary_siliconflow_backup(cfg):
    """提炼链顺序（2026-08-29 B121）：agnes 主 → siliconflow 备 → rule drop_batch 兜底。

    zhipu 免费节点已移出（9af882b）；锚定 config/llm.yaml 现链。
    """
    chain = cfg["llm"]["chains"]["refinement"]
    providers = [n.get("provider") for n in chain]
    assert providers == ["agnes", "siliconflow", "rule"]
    assert chain[0]["model"] == "agnes-2.5-flash"
    assert chain[1]["model"] == "deepseek-ai/DeepSeek-V4-Flash"
    assert chain[-1].get("rule") == "drop_batch"
