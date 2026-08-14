"""T-7 测试：滑动窗口限流中间件（SGME-接口契约-v0.1.md §6 限流定稿）。

覆盖：
- 同一 key 连续请求超过阈值 → 429 + Retry-After 头（ERR_RATE_LIMITED）
- /v1/health 豁免（大量请求仍 200）
- rate_limit_per_min=0 → 不限制
- 不同 key 独立计数
- 无 X-API-Key 的请求不计限流（交后续鉴权返回 401/403）
- 限流器单元行为（滑动窗口 / 0=关闭 / 独立计数）
"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine import health as health_mod
from sgme.server import ratelimit as rl
from sgme.server.app import create_app

# 不存在路径：仅用于验证限流计数，不经路由鉴权（返回 404，但中间件已计数）
PROBE = "/v1/ratelimit-probe"


@pytest.fixture
def base_cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, base_cfg):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, base_cfg["dimensions"], base_cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def _make_app(base_cfg, conns, rate_limit_per_min, tmp_path, monkeypatch, mock_llm=True):
    """构建一个注入限流阈值的应用（隔离 DB + 关闭 Bearer 以免 401 干扰）。"""
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("server", {})["rate_limit_per_min"] = rate_limit_per_min
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    if mock_llm:
        monkeypatch.setattr(
            health_mod, "check_llm_available",
            lambda c, client=None: {"available": True, "provider": "mock",
                                     "model": "mock", "error": None},
        )
    mem_conn, session_conn, wiki_conn = conns
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin",
        agent_key="test-agent",
        bearer_token="",  # 显式关闭 Bearer，避免 401 干扰限流验证
        agent_store_path=tmp_path / "agent_keys.json",
    )
    return app


# ---------- 集成：限流行为 ----------

def test_same_key_over_limit_returns_429_with_retry_after(base_cfg, conns, tmp_path, monkeypatch):
    """同一 key 连续请求超过阈值 → 429 + Retry-After 头（ERR_RATE_LIMITED）。"""
    limit = 3
    client = TestClient(_make_app(base_cfg, conns, limit, tmp_path, monkeypatch, mock_llm=False))
    headers = {"X-API-Key": "agent-k1"}
    # 前 limit 次放行（路径不存在 → 404，但未被限流）
    for i in range(limit):
        r = client.get(PROBE, headers=headers)
        assert r.status_code != 429, f"第 {i + 1} 次不应被限流"
    # 第 limit+1 次超限
    r = client.get(PROBE, headers=headers)
    assert r.status_code == 429
    assert r.headers.get("Retry-After") is not None
    assert int(r.headers["Retry-After"]) >= 1
    body = r.json()
    assert body["error"]["code"] == "ERR_RATE_LIMITED"


def test_health_exempt_from_rate_limit(base_cfg, conns, tmp_path, monkeypatch):
    """/v1/health 豁免：远超阈值的大量请求仍全部 200。"""
    limit = 2
    client = TestClient(_make_app(base_cfg, conns, limit, tmp_path, monkeypatch, mock_llm=True))
    headers = {"X-API-Key": "agent-health"}
    for _ in range(20):
        r = client.get("/v1/health", headers=headers)
        assert r.status_code == 200, r.text


def test_rate_limit_zero_disabled(base_cfg, conns, tmp_path, monkeypatch):
    """rate_limit_per_min=0 → 关闭限流（大量请求不 429）。"""
    client = TestClient(_make_app(base_cfg, conns, 0, tmp_path, monkeypatch, mock_llm=False))
    headers = {"X-API-Key": "agent-zero"}
    for _ in range(50):
        r = client.get(PROBE, headers=headers)
        assert r.status_code != 429, "rate_limit_per_min=0 应关闭限流"


def test_different_keys_independent(base_cfg, conns, tmp_path, monkeypatch):
    """不同 key 独立计数：keyA 超限不影响 keyB 放行。"""
    limit = 3
    client = TestClient(_make_app(base_cfg, conns, limit, tmp_path, monkeypatch, mock_llm=False))
    kA = {"X-API-Key": "agent-A"}
    kB = {"X-API-Key": "agent-B"}
    for _ in range(limit):
        assert client.get(PROBE, headers=kA).status_code != 429
    # keyA 超限
    rA = client.get(PROBE, headers=kA)
    assert rA.status_code == 429
    # keyB 独立计数，仍放行
    rB = client.get(PROBE, headers=kB)
    assert rB.status_code != 429


def test_no_key_not_rate_limited(base_cfg, conns, tmp_path, monkeypatch):
    """无 X-API-Key 的请求不计限流（交后续鉴权返回 401/403，而非 429）。"""
    limit = 1
    client = TestClient(_make_app(base_cfg, conns, limit, tmp_path, monkeypatch, mock_llm=False))
    for _ in range(10):
        r = client.get(PROBE)
        assert r.status_code != 429


# ---------- 单元：限流器本身 ----------

def test_sliding_window_unit_behavior():
    """滑动窗口：达到阈值后拒绝；0=关闭；不同 key 独立。"""
    lim = rl.SlidingWindowRateLimiter(limit_per_min=2, window_seconds=60)
    assert lim.is_allowed("k")[0] is True
    assert lim.is_allowed("k")[0] is True
    allowed, retry = lim.is_allowed("k")
    assert allowed is False
    assert retry >= 0
    # 独立 key 不受前者影响
    assert lim.is_allowed("other")[0] is True
    # 0 = 关闭
    assert rl.SlidingWindowRateLimiter(limit_per_min=0).is_allowed("x")[0] is True


def test_resolve_limit_default_and_zero():
    """_resolve_limit：缺失→默认 120；0→0（关闭）；非法→默认 120。"""
    assert rl._resolve_limit(None) == 120
    assert rl._resolve_limit({"server": {"rate_limit_per_min": 0}}) == 0
    assert rl._resolve_limit({"server": {"rate_limit_per_min": 7}}) == 7
    assert rl._resolve_limit({"server": {"rate_limit_per_min": -1}}) == 120
    assert rl._resolve_limit({"server": "bad"}) == 120
