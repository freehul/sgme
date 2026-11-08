# -*- coding: utf-8 -*-
"""W06/W07：图召回纳入 2.0 归属边（belongs_to）与因果边（causes）+ 结果上下文回填。

覆盖：
1. 配置默认口径：exclude_relations 只排 contradicts；belongs_to(0.3)/causes(1.0) 权重；
2. _graph_candidates 纳入 belongs_to(l2_extract) 与 causes（双向），排除 contradicts；
3. 其他关系（similar / supersedes / system belongs_to）不被误排；
4. top_n 上限（候选截断）与默认 20；
5. 无边时向后兼容（候选空、结果与图关逐字节等价）；
6. 融合路径（向量关 + 图开）下，最终结果仍带 facts/causes（_enrich_l2_context 生效）。

零真实 DB：tmp_path 建库 + 手工合成边（不依赖 backend 写入）。
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.config import DEFAULT_SEARCH_CONFIG
from sgme.data import db as db_mod
from sgme.data import edge_dao
from sgme.data import search as search_mod

L2_MAIN = {
    "facts": [{"type": "偏好", "content": "按钮统一白字", "subject": "self"}],
    "causes": [{"from": "对比度不足", "to": "按钮统一白字"}],
}
L2_NEIGHBOR = {
    "facts": [{"type": "约束", "content": "禁用低对比方案", "subject": "self"}],
    "causes": [],
}


# ---------- 测试库构造（照抄 test_search_graph.py 的轻量 helper） ----------

def _conn(tmp_path) -> sqlite3.Connection:
    return db_mod.connect_memory(tmp_path)


def _mem(conn, mid: str, content: str | None = None, *, l2: str | None = None,
         priority: int = 50, status: str = "active",
         lifecycle: str | None = None) -> None:
    import json

    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "ttl_days, created_at, updated_at, status, occurred_at, l2_facts_json, lifecycle) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, content or mid, "fact", priority, "dynamic", None,
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", status,
         "2026-01-01T00:00:00Z", json.dumps(l2, ensure_ascii=False) if l2 else None,
         lifecycle),
    )


def _edge(conn, frm: str, to: str, relation: str, weight: float = 1.0,
          source: str = "l2_extract") -> None:
    edge_dao.create_edge(conn, frm, to, relation, weight=weight, source=source)


def _cands(conn, seed: str, *, graph_cfg: dict | None = None) -> list[dict]:
    cfg = {"search": {"graph": {"enabled": True, **(graph_cfg or {})}}}
    return search_mod._graph_candidates(conn, [{"memory_id": seed}], [], cfg)


# ---------- 1. 配置默认口径 ----------

def test_graph_config_defaults_only_exclude_contradicts():
    """默认只排 contradicts；belongs_to/causes 均参与（权重按关系级缩放）。"""
    graph = DEFAULT_SEARCH_CONFIG["graph"]
    assert graph["exclude_relations"] == ["contradicts"]
    assert "belongs_to" not in graph["exclude_relations"]
    assert "causes" not in graph["exclude_relations"]
    assert graph["relation_weights"]["belongs_to"] == 0.3
    assert "causes" not in graph["relation_weights"]
    assert graph["top_n"] == 20
    assert graph["fill_only"] is True


# ---------- 2. 纳入 2.0 归属边与因果边 ----------

def test_graph_candidates_include_l2_belongs_to_and_causes(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "g1", "GraphSeedToken")
    _mem(conn, "g2", "GraphCauseToken")
    _mem(conn, "g3", "GraphParentToken")
    _mem(conn, "g4", "GraphNegToken")
    _edge(conn, "g1", "g2", "causes", weight=1.0)                  # 出边（因果）
    _edge(conn, "g3", "g1", "belongs_to", weight=1.0)              # 入边（归属：g3 是父）
    _edge(conn, "g1", "g4", "contradicts", weight=0.9, source="l1_conflict")
    conn.commit()

    cands = _cands(conn, "g1")

    by_id = {c["memory_id"]: c["score"] for c in cands}
    assert set(by_id) == {"g2", "g3"}          # belongs_to + causes 都被纳入
    assert by_id["g2"] == pytest.approx(1.0)    # causes 不缩放
    assert by_id["g3"] == pytest.approx(0.3)    # belongs_to ×0.3（关系级尺度压缩）
    assert [c["memory_id"] for c in cands] == ["g2", "g3"]  # 因果边权重高排前
    db_mod.close(conn)


def test_graph_candidates_keep_other_relations(tmp_path):
    """只排 contradicts：similar / supersedes / system belongs_to 照常参与。"""
    conn = _conn(tmp_path)
    _mem(conn, "h1", "GraphKeepToken seed")
    for i in (2, 3, 4, 5):
        _mem(conn, f"h{i}", f"GraphKeepToken {i}")
    _edge(conn, "h1", "h2", "similar", weight=0.8, source="l1_conflict")
    _edge(conn, "h1", "h3", "supersedes", weight=1.0, source="system")
    _edge(conn, "h1", "h4", "belongs_to", weight=2.0, source="system")
    _edge(conn, "h1", "h5", "contradicts", weight=0.9, source="l1_conflict")
    conn.commit()

    ids = {c["memory_id"] for c in _cands(conn, "h1")}

    assert ids == {"h2", "h3", "h4"}       # 只有 contradicts 被排除
    db_mod.close(conn)


def test_graph_exclude_relations_explicit_only_contradicts(tmp_path):
    """显式给 exclude_relations=["contradicts"]（生产缺省值）时同样保留两类新边。"""
    conn = _conn(tmp_path)
    _mem(conn, "k1", "GraphExplicitToken seed")
    _mem(conn, "k2", "GraphExplicitToken 因果")
    _mem(conn, "k3", "GraphExplicitToken 归属")
    _edge(conn, "k1", "k2", "causes")
    _edge(conn, "k1", "k3", "belongs_to")
    conn.commit()

    ids = {c["memory_id"] for c in _cands(
        conn, "k1", graph_cfg={"exclude_relations": ["contradicts"]})}

    assert ids == {"k2", "k3"}
    db_mod.close(conn)


# ---------- 3. 上限（top_n） ----------

def test_graph_candidates_top_n_cap(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "t0", "GraphCapToken seed")
    for i in range(1, 31):
        _mem(conn, f"t{i}", f"GraphCapToken {i}")
        _edge(conn, "t0", f"t{i}", "causes", weight=float(i))
    conn.commit()

    cands = _cands(conn, "t0", graph_cfg={"top_n": 5})

    assert len(cands) == 5
    assert {c["memory_id"] for c in cands} == {"t26", "t27", "t28", "t29", "t30"}
    assert [c["score"] for c in cands] == sorted(
        [c["score"] for c in cands], reverse=True)
    # 缺省 top_n=20 同样生效
    assert len(_cands(conn, "t0")) == 20
    db_mod.close(conn)


# ---------- 3b. supersession 归档（lifecycle='archived'）不进图候选 ----------

def test_graph_candidates_exclude_archived_lifecycle(tmp_path):
    """supersession 只改 lifecycle、不删行 → 边仍在；候选侧必须按 lifecycle 排除。

    W07 口径（lead 裁决）：lifecycle='archived' 不参与图召回；
    sleeping / NULL 仍可召回（sleeping 只是 TTL 休眠，检索可见）。
    """
    conn = _conn(tmp_path)
    _mem(conn, "a1", "GraphLifeToken seed")
    _mem(conn, "a2", "GraphLifeToken 在用")
    _mem(conn, "a3", "GraphLifeToken 已被替代", lifecycle="archived")
    _mem(conn, "a4", "GraphLifeToken 休眠", lifecycle="sleeping")
    _edge(conn, "a1", "a2", "causes")
    _edge(conn, "a1", "a3", "causes")   # superseded：边还在，但候选要排除
    _edge(conn, "a1", "a4", "causes")
    conn.commit()

    ids = {c["memory_id"] for c in _cands(conn, "a1")}

    assert ids == {"a2", "a4"}
    db_mod.close(conn)


# ---------- 4. 无边时向后兼容 ----------

def test_graph_no_edges_backward_compatible(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "n1", "GraphNoEdgeToken 唯一命中")
    _mem(conn, "n2", "GraphNoEdgeToken 另一条")
    conn.commit()

    cfg_on = {"search": {"vector": {"enabled": False},
                         "graph": {"enabled": True}}}
    cfg_off = {"search": {"vector": {"enabled": False},
                          "graph": {"enabled": False}}}
    assert search_mod._graph_candidates(conn, [{"memory_id": "n1"}], [], cfg_on) == []
    on = search_mod.search_memories(conn, None, query="GraphNoEdgeToken",
                                    limit=10, include_sources=False, cfg=cfg_on)
    off = search_mod.search_memories(conn, None, query="GraphNoEdgeToken",
                                     limit=10, include_sources=False, cfg=cfg_off)
    assert on == off
    assert all("graph" not in r["routes"] for r in on)
    db_mod.close(conn)


# ---------- 5. 融合路径仍带 facts/causes ----------

def test_search_memories_l2_edges_results_have_facts_causes(tmp_path):
    """向量关 + 图开 → 走 RRF 融合（会丢原始列）；结果仍须带 facts/causes。"""
    conn = _conn(tmp_path)
    _mem(conn, "f1", "GraphFusionToken 主干", l2=L2_MAIN)
    _mem(conn, "f2", "GraphFusionNeighborToken 邻居", l2=L2_NEIGHBOR)
    _edge(conn, "f1", "f2", "causes", weight=1.0)
    conn.commit()

    cfg = {"search": {"vector": {"enabled": False},
                      "graph": {"enabled": True, "weight": 8.0}}}
    results = search_mod.search_memories(conn, None, query="GraphFusionToken",
                                        limit=10, include_sources=False, cfg=cfg)

    by_id = {r["memory_id"]: r for r in results}
    assert set(by_id) == {"f1", "f2"}
    assert "graph" in by_id["f1"]["routes"]
    # 融合后每条都带 facts/causes（上下文回填在最终结果集上做，与融合路径解耦）
    assert [f["content"] for f in by_id["f1"]["facts"]] == ["按钮统一白字"]
    assert by_id["f1"]["causes"] == [{"from": "对比度不足", "to": "按钮统一白字"}]
    assert [f["content"] for f in by_id["f2"]["facts"]] == ["禁用低对比方案"]
    assert by_id["f2"]["causes"] == []
    # 原始列不泄漏
    for r in results:
        assert "facts_json" not in r and "l2_facts_json" not in r
    db_mod.close(conn)


def test_search_graph_off_with_l2_edges_no_graph_route(tmp_path):
    conn = _conn(tmp_path)
    _mem(conn, "q1", "GraphOffToken 主干", l2=L2_MAIN)
    _mem(conn, "q2", "GraphOffNeighborToken 邻居", l2=L2_NEIGHBOR)
    _edge(conn, "q1", "q2", "causes")
    conn.commit()

    cfg = {"search": {"vector": {"enabled": False},
                      "graph": {"enabled": False}}}
    results = search_mod.search_memories(conn, None, query="GraphOffToken",
                                        limit=10, include_sources=False, cfg=cfg)

    assert [r["memory_id"] for r in results] == ["q1"]   # 图关：邻居不进结果
    assert "graph" not in results[0]["routes"]
    assert results[0]["causes"] == [{"from": "对比度不足", "to": "按钮统一白字"}]
    db_mod.close(conn)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
