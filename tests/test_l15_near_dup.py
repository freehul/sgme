"""L1.5 F1 近似查重（near_dup）测试：配置段（T1.4）+ 判定四分支（T1.5）。

L1.5 P1（T-167 词法扩展）：_exact_dup_check 只拦完全相同 content，换措辞重复
漏过率 4.5%（副本库 400 条抽样 18 条）。F1 在落库前做全池向量近似检索（limit=1）：
score≥threshold 命中 → 内容进隔离表人工复核（不静默 skip）；灰区 [gray_zone, threshold)
只聚合计数 + 明细抽样（阈值校准回算用）。全部用例零网络：embed / vector_search 一律 mock。
"""

from __future__ import annotations

import logging

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao
from sgme.data.search import vector as vector_mod
from sgme.engine import l15


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _nd_cfg(enabled=True, threshold=0.92, gray_zone=0.85) -> dict:
    """构造 l15.near_dup 配置（判定用例默认出厂示值）。"""
    return {"l15": {"near_dup": {"enabled": enabled, "threshold": threshold,
                                 "gray_zone": gray_zone}}}


# ---------- 0. 配置透传与回退（T1.4） ----------

def test_load_config_exposes_l15_near_dup():
    """load_config 组装结果含 l15.near_dup（包内基线透传 + 三键齐全）。"""
    # SGME_HOME 隔离下默认路径已重定向到 tmp，显式指包内真实配置验证透传
    cfg = config.load_config(
        sgme_path=config.RESOURCE_ROOT / "config" / "sgme.yaml"
    )
    l15_cfg = cfg.get("l15", {})
    assert "near_dup" in l15_cfg
    nd = l15_cfg["near_dup"]
    # 出厂保守示值（部署覆盖层由主仓同步显式 true）
    assert nd["enabled"] is True
    assert nd["threshold"] == 0.92
    assert nd["gray_zone"] == 0.85


def test_near_dup_cfg_fallback_defaults():
    """空/缺段配置 → 出厂保守值；单键类型非法 → 该键回退（不静默降档）。"""
    factory = {"enabled": True, "threshold": 0.92, "gray_zone": 0.85}
    assert l15._near_dup_cfg(None) == factory
    assert l15._near_dup_cfg({}) == factory
    assert l15._near_dup_cfg({"l15": {}}) == factory
    # 部分键：enabled=false 生效，其余键回退出厂值
    assert l15._near_dup_cfg({"l15": {"near_dup": {"enabled": False}}}) == {
        "enabled": False, "threshold": 0.92, "gray_zone": 0.85,
    }
    # 类型非法：字符串/布尔 → 对应键回退出厂值
    assert l15._near_dup_cfg(
        {"l15": {"near_dup": {"threshold": "高", "gray_zone": True}}}
    ) == factory


def test_near_dup_cfg_invalid_fallback(caplog):
    """gray_zone >= threshold 非法 → WARNING + 回退全组出厂值。"""
    caplog.set_level(logging.WARNING, logger="sgme.engine.l15")
    nd = l15._near_dup_cfg(
        {"l15": {"near_dup": {"enabled": False, "threshold": 0.8, "gray_zone": 0.9}}}
    )
    assert nd == {"enabled": True, "threshold": 0.92, "gray_zone": 0.85}
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("near_dup" in m and "回退" in m for m in warns), warns


def test_config_merge_l15_near_dup():
    """_merge_l15_config：near_dup 默认段随白名单合并 + 逐键透传 + 防可变默认共享。"""
    base = config._merge_l15_config(None)
    assert base["near_dup"] == {"enabled": True, "threshold": 0.92, "gray_zone": 0.85}
    merged = config._merge_l15_config({"near_dup": {"threshold": 0.9}})
    assert merged["near_dup"] == {"enabled": True, "threshold": 0.9, "gray_zone": 0.85}
    # 防可变默认共享：合并产物不是默认段的同一对象
    assert base["near_dup"] is not config.DEFAULT_L15_CONFIG["near_dup"]


# ---------- 1. _near_dup_check 四分支（T1.5） ----------

def test_near_dup_embed_failed_passes(mem_conn, monkeypatch, caplog):
    """分支1：embed 不可达（None）→ 放行 + WARNING near_dup embed failed。"""
    caplog.set_level(logging.WARNING, logger="sgme.engine.l15")
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: None)
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: pytest.fail("不应调用"))

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg())
    assert res.hit is False and res.gray is False and res.matched_id is None
    warns = [r.getMessage() for r in caplog.records]
    assert any("near_dup embed failed" in m for m in warns), warns


def test_near_dup_embed_exception_passes(mem_conn, monkeypatch, caplog):
    """分支1 变体：embed 抛异常 → 放行 + WARNING（异常形态带 : %s）。"""
    caplog.set_level(logging.WARNING, logger="sgme.engine.l15")

    def boom(*a, **kw):
        raise RuntimeError("embed 端点挂了")

    monkeypatch.setattr(vector_mod, "embed", boom)
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: pytest.fail("不应调用"))

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg())
    assert res.hit is False and res.gray is False
    warns = [r.getMessage() for r in caplog.records]
    assert any("near_dup embed failed" in m for m in warns), warns


def test_near_dup_empty_search_passes(mem_conn, monkeypatch):
    """分支2：检索空 → 放行（正常）。"""
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: [])

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg())
    assert res.hit is False and res.gray is False and res.matched_id is None


def test_near_dup_search_exception_passes(mem_conn, monkeypatch, caplog):
    """分支3：vector_search 抛异常 → 放行 + WARNING near_dup vector_search failed。"""
    caplog.set_level(logging.WARNING, logger="sgme.engine.l15")
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])

    def boom(*a, **kw):
        raise RuntimeError("vector_search 挂了")

    monkeypatch.setattr(vector_mod, "vector_search", boom)

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg())
    assert res.hit is False and res.gray is False
    warns = [r.getMessage() for r in caplog.records]
    assert any("near_dup vector_search failed" in m for m in warns), warns


def test_near_dup_hit_over_threshold(mem_conn, monkeypatch):
    """分支4a：score ≥ threshold(0.92) → hit=True，携带 matched_id/score。"""
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: [
        {"memory_id": "mem-hit-1", "content": "旧记忆", "priority": 60,
         "updated_at": "2026-01-01T00:00:00Z", "score": 0.95},
    ])

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg())
    assert res.hit is True and res.gray is False
    assert res.matched_id == "mem-hit-1"
    assert res.score == 0.95


def test_near_dup_gray_zone(mem_conn, monkeypatch):
    """分支4b：0.85 ≤ score(0.88) < 0.92 → hit=False, gray=True（放行落库但可观测）。"""
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: [
        {"memory_id": "mem-gray-1", "content": "旧记忆", "priority": 60,
         "updated_at": "2026-01-01T00:00:00Z", "score": 0.88},
    ])

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg())
    assert res.hit is False and res.gray is True
    assert res.matched_id == "mem-gray-1"
    assert res.score == 0.88


def test_near_dup_below_gray_zone_passes(mem_conn, monkeypatch):
    """分支4c：score < gray_zone(0.85) → 放行（无命中信号）。"""
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: [
        {"memory_id": "mem-low-1", "content": "旧记忆", "priority": 60,
         "updated_at": "2026-01-01T00:00:00Z", "score": 0.5},
    ])

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg())
    assert res.hit is False and res.gray is False and res.matched_id is None


def test_near_dup_search_uses_limit_1(mem_conn, monkeypatch):
    """判定只用最高分：vector_search 实参 limit=1（实参断言）。"""
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    captured: dict = {}

    def fake_vs(conn, vec, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(vector_mod, "vector_search", fake_vs)
    l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg())
    assert captured.get("limit") == 1


def test_near_dup_precomputed_sim_low_skips_search(mem_conn, monkeypatch):
    """F3 复用位：precomputed_sim < gray_zone → 直接放行，免 embed 与检索（计数证否）。"""
    calls = {"embed": 0, "search": 0}

    def fake_embed(*a, **kw):
        calls["embed"] += 1
        return [0.1, 0.2, 0.3]

    def fake_vs(*a, **kw):
        calls["search"] += 1
        return []

    monkeypatch.setattr(vector_mod, "embed", fake_embed)
    monkeypatch.setattr(vector_mod, "vector_search", fake_vs)

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg(), precomputed_sim=0.3)
    assert res.hit is False and res.gray is False
    assert calls["search"] == 0  # 不调 vector_search
    assert calls["embed"] == 0   # 直接放行，连 embed 都免


def test_near_dup_precomputed_sim_high_goes_normal_search(mem_conn, monkeypatch):
    """precomputed_sim ≥ gray_zone → 走正常检索（F1 需 matched_id 与精确分数）。"""
    calls = {"search": 0}
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])

    def fake_vs(*a, **kw):
        calls["search"] += 1
        return [{"memory_id": "mem-pc-1", "content": "旧记忆", "priority": 60,
                 "updated_at": "2026-01-01T00:00:00Z", "score": 0.93}]

    monkeypatch.setattr(vector_mod, "vector_search", fake_vs)
    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg(), precomputed_sim=0.9)
    assert calls["search"] == 1
    assert res.hit is True and res.matched_id == "mem-pc-1"


def test_near_dup_disabled_passes(mem_conn, monkeypatch):
    """开关关闭（enabled=false）→ 直接放行，不调 embed/检索。"""
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: pytest.fail("不应调用"))
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: pytest.fail("不应调用"))

    res = l15._near_dup_check(mem_conn, "新记忆内容", _nd_cfg(enabled=False))
    assert res.hit is False and res.gray is False


def test_near_dup_gray_sample_log(mem_conn, monkeypatch, caplog):
    """分支4b 观测：灰区明细日志 10% 采样（random<0.1 时输出 score/matched_id/content 前 80 字）。"""
    caplog.set_level(logging.INFO, logger="sgme.engine.l15")
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: [
        {"memory_id": "mem-gray-2", "content": "旧记忆", "priority": 60,
         "updated_at": "2026-01-01T00:00:00Z", "score": 0.87},
    ])
    monkeypatch.setattr(l15.random, "random", lambda: 0.05)  # 命中 10% 采样窗口
    content = "用于抽样日志断言的新记忆内容" * 12  # > 80 字，验证截断

    l15._near_dup_check(mem_conn, content, _nd_cfg())
    hits = [r.getMessage() for r in caplog.records if "灰区" in r.getMessage()]
    assert hits, "应输出灰区抽样日志"
    msg = hits[0]
    assert "0.87" in msg and "mem-gray-2" in msg
    assert content[:80] in msg  # content 截断到前 80 字
