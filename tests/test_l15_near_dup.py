"""L1.5 F1 近似查重（near_dup）测试：配置段（T1.4）+ 判定四分支（T1.5）+ 接线集成（T1.6）。

L1.5 P1（T-167 词法扩展）：_exact_dup_check 只拦完全相同 content，换措辞重复
漏过率 4.5%（副本库 400 条抽样 18 条）。F1 在落库前做全池向量近似检索（limit=1）：
score≥threshold 命中 → 内容进隔离表人工复核（不静默 skip）；灰区 [gray_zone, threshold)
只聚合计数 + 明细抽样（阈值校准回算用）。全部用例零网络：embed / vector_search 一律 mock。
T1.6 集成档：resolve_conflicts 4 处落库点（全空短路 / 批 store / update 退化 / merge 退化）
+ near_dup 独立计数记录 + 第五档（隔离表写入异常不阻塞）。
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, near_dup_dao
from sgme.data.refine_dao import RefineRunRecorder
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


# ---------- 2. resolve_conflicts 接线（T1.6：4 处落库点 + 独立计数记录） ----------

#: 集成用例配置：关 prescreen（候选走维度召回，零 embed 依赖）+ 出厂 near_dup 三键
_INTEG_L15_CFG = {
    "prescreen": None,
    "near_dup": {"enabled": True, "threshold": 0.92, "gray_zone": 0.85},
}


def _apply_integ_cfg(cfg) -> dict:
    """隔离集成用例配置（深拷贝一层，避免跨用例共享可变默认）。"""
    cfg["l15"] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _INTEG_L15_CFG.items()}
    return cfg


def _new_mem(content: str) -> dict:
    return {
        "content": content,
        "dimension_ids": ["goals"],
        "memory_type": "persona",
        "priority": 50,
        "time_velocity": "static",
        "source_message_ids": [],
        "supersedes": [],
    }


def _seed_existing(mem_conn, content: str = "既有维度记忆内容") -> str:
    """插入一条与新记忆同维度（goals）的旧记忆 → 候选池非空（走批裁决路径）。"""
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["goals"],
    )


def _mock_vs_hit(memory_id: str, score: float):
    """vector_search mock：返回单条命中（limit=1 语义）。"""
    return lambda *a, **kw: [
        {"memory_id": memory_id, "content": "旧记忆", "priority": 60,
         "updated_at": "2026-01-01T00:00:00Z", "score": score},
    ]


def _decisions_client(decisions: list[dict]) -> httpx.Client:
    """mock LLM：返回给定裁决 JSON（照 test_l15_idempotent 的 MockTransport 形状）。"""
    body = json.dumps(decisions)

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _count_content(conn, content: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE TRIM(content)=?", (content,)
    ).fetchone()["c"]


def _runs(mem_conn) -> list[dict]:
    return RefineRunRecorder.list_by_stage(mem_conn, "l1_conflict")


def _near_dup_rows(mem_conn) -> list[dict]:
    return [r for r in _runs(mem_conn) if r["provider"] == "near_dup"]


def test_integration_empty_pool_hit_quarantines(mem_conn, cfg, monkeypatch):
    """站点①（全空短路分支）：近似命中 → 不落 memories、写隔离表、独立计数记录对账。"""
    _apply_integ_cfg(cfg)
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", _mock_vs_hit("mem-old-0001", 0.95))

    content = "近似重复的新内容 A"
    res = l15.resolve_conflicts([_new_mem(content)], mem_conn, cfg, source_ref="F:1")

    assert res.stored == [] and res.skipped == [0]
    assert res.near_dup_quarantined == 1 and res.near_dup_gray == 0
    assert _count_content(mem_conn, content) == 0
    # 隔离表有行（内容 / matched_id / score / source_ref / dimension_ids 全字段）
    items = near_dup_dao.list_items(mem_conn)
    assert len(items) == 1
    it = items[0]
    assert it["content"] == content
    assert it["matched_memory_id"] == "mem-old-0001"
    assert it["score"] == 0.95
    assert it["source_ref"] == "F:1"
    assert it["dimension_ids"] == ["goals"]
    assert it["status"] == "pending"
    # refine_runs：near_dup 独立记录（action_counts 对账）
    nd_rows = _near_dup_rows(mem_conn)
    assert len(nd_rows) == 1
    assert json.loads(nd_rows[0]["action_counts"]) == {"near_dup_quarantined": 1, "near_dup_gray": 0}
    assert nd_rows[0]["memories_count"] == 1
    assert nd_rows[0]["status"] == "ok"
    # T1.7：同分支另写 empty_pool 独立记录（stored_n=0），二者共存（写入顺序无关）
    ep_rows = [r for r in _runs(mem_conn) if r["provider"] == "empty_pool"]
    assert len(ep_rows) == 1
    assert json.loads(ep_rows[0]["action_counts"]) == {"empty_pool_store": 0}


def test_integration_batch_store_path_quarantines(mem_conn, cfg, monkeypatch):
    """站点②（批裁决 store 分支）：命中照拦；批裁决正常记录 + near_dup 独立记录并存。"""
    _apply_integ_cfg(cfg)
    old_id = _seed_existing(mem_conn)
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", _mock_vs_hit(old_id, 0.95))
    client = _decisions_client([
        {"new_memory_index": 0, "candidate_ids": [old_id], "action": "store"},
    ])

    content = "批路径命中内容 B"
    res = l15.resolve_conflicts([_new_mem(content)], mem_conn, cfg, client=client)

    assert res.stored == [] and res.skipped == [0]
    assert res.near_dup_quarantined == 1
    assert _count_content(mem_conn, content) == 0
    assert len(near_dup_dao.list_items(mem_conn)) == 1
    nd_rows = _near_dup_rows(mem_conn)
    assert len(nd_rows) == 1
    assert json.loads(nd_rows[0]["action_counts"]) == {"near_dup_quarantined": 1, "near_dup_gray": 0}
    # T1.7：批行候选留痕三列——candidate_count / ids_hash 与批候选集一致（reason 不传 → None）
    batch_rows = [r for r in _runs(mem_conn) if r["candidate_count"] is not None]
    assert len(batch_rows) == 1
    assert batch_rows[0]["candidate_count"] == 1
    assert batch_rows[0]["candidate_ids_hash"] == l15._candidate_ids_hash([old_id])
    assert batch_rows[0]["short_circuit_reason"] is None


def test_integration_update_fallback_quarantines(mem_conn, cfg, monkeypatch):
    """站点③（update 退化 store，candidate_ids=[]）：命中照拦，无 update 发生。"""
    _apply_integ_cfg(cfg)
    _seed_existing(mem_conn)
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", _mock_vs_hit("mem-old-0002", 0.93))
    client = _decisions_client([
        {"new_memory_index": 0, "candidate_ids": [], "action": "update", "reason": "退化"},
    ])

    content = "update 退化命中内容 C"
    res = l15.resolve_conflicts([_new_mem(content)], mem_conn, cfg, client=client)

    assert res.updated == [] and res.stored == [] and res.skipped == [0]
    assert res.near_dup_quarantined == 1
    assert _count_content(mem_conn, content) == 0
    assert len(near_dup_dao.list_items(mem_conn)) == 1
    assert len(_near_dup_rows(mem_conn)) == 1


def test_integration_merge_fallback_quarantines(mem_conn, cfg, monkeypatch):
    """站点④（merge 退化 store，无 merged_content）：命中照拦，无 merge 发生。"""
    _apply_integ_cfg(cfg)
    _seed_existing(mem_conn)
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", _mock_vs_hit("mem-old-0003", 0.94))
    client = _decisions_client([
        {"new_memory_index": 0, "candidate_ids": [], "action": "merge", "reason": "退化"},
    ])

    content = "merge 退化命中内容 D"
    res = l15.resolve_conflicts([_new_mem(content)], mem_conn, cfg, client=client)

    assert res.merged == [] and res.stored == [] and res.skipped == [0]
    assert res.near_dup_quarantined == 1
    assert _count_content(mem_conn, content) == 0
    assert len(near_dup_dao.list_items(mem_conn)) == 1
    assert len(_near_dup_rows(mem_conn)) == 1


def test_integration_gray_zone_passes_with_count(mem_conn, cfg, monkeypatch):
    """灰区：[0.85, 0.92) → 记忆照常 store + near_dup_gray 计数；隔离表无行；记录对账。"""
    _apply_integ_cfg(cfg)
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", _mock_vs_hit("mem-old-0004", 0.88))

    content = "灰区放行内容 E"
    res = l15.resolve_conflicts([_new_mem(content)], mem_conn, cfg)

    assert len(res.stored) == 1 and res.skipped == []
    assert res.near_dup_gray == 1 and res.near_dup_quarantined == 0
    assert _count_content(mem_conn, content) == 1
    assert near_dup_dao.list_items(mem_conn) == []
    nd_rows = _near_dup_rows(mem_conn)
    assert len(nd_rows) == 1
    assert json.loads(nd_rows[0]["action_counts"]) == {"near_dup_quarantined": 0, "near_dup_gray": 1}
    assert nd_rows[0]["memories_count"] == 1


def test_integration_quarantine_write_failure_not_blocking(mem_conn, cfg, monkeypatch, caplog):
    """第五档：隔离表写入异常 → 主流程不崩、skip 照旧、WARNING 含 content 全文。"""
    _apply_integ_cfg(cfg)
    caplog.set_level(logging.WARNING, logger="sgme.engine.l15")
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", _mock_vs_hit("mem-old-0005", 0.95))

    def boom(*a, **kw):
        raise RuntimeError("隔离表不可写")

    monkeypatch.setattr(near_dup_dao, "upsert_hit", boom)

    content = "隔离表写入失败仍需 skip 的内容 F"
    res = l15.resolve_conflicts([_new_mem(content)], mem_conn, cfg)

    assert res.stored == [] and res.skipped == [0]
    assert res.near_dup_quarantined == 1
    assert near_dup_dao.list_items(mem_conn) == []
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("隔离表写入失败" in m and content in m for m in warns), warns


# ---------- 3. empty_pool 独立记录（T1.7：全空短路路径承载） ----------

def test_integration_empty_pool_record_written(mem_conn, cfg, monkeypatch):
    """T1.7：空库 + embed 不可达放行 → 正常 store；empty_pool 记录 action_counts 对账。"""
    _apply_integ_cfg(cfg)
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: None)  # 端到端不可达 → F1 fail-open
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: pytest.fail("不应调用"))

    content = "空池直存内容 G"
    res = l15.resolve_conflicts([_new_mem(content)], mem_conn, cfg)

    assert len(res.stored) == 1 and res.skipped == []
    assert res.near_dup_quarantined == 0 and res.near_dup_gray == 0
    assert _count_content(mem_conn, content) == 1
    # empty_pool 独立记录（本分支执行即写，承载零候选事实 + 实际 store 数）
    ep_rows = [r for r in _runs(mem_conn) if r["provider"] == "empty_pool"]
    assert len(ep_rows) == 1
    assert json.loads(ep_rows[0]["action_counts"]) == {"empty_pool_store": 1}
    assert ep_rows[0]["memories_count"] == 1
    assert ep_rows[0]["status"] == "ok"
    # 无近似信号 → 不写 near_dup 记录（两记录独立，互不依赖）
    assert _near_dup_rows(mem_conn) == []
