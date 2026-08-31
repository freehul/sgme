# -*- coding: utf-8 -*-
"""search 图召回 v1（ST-38 T-134）测试。

覆盖：_graph_candidates（种子去重/增量/active 过滤/图不可用降级）、
search_memories 集成（图开=邻居进结果且 routes 含 graph；图关=逐字节等价；
无边=等价；权重影响排序）。
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import edge_dao
from sgme.data import search as search_mod
from sgme.data.search import rrf as rrf_mod


# ---------- 测试库构造（复用 test_edge_dao 的轻量 helper） ----------

def _conn(tmp_path) -> sqlite3.Connection:
    return db_mod.connect_memory(tmp_path)


# 内容词互不共享子串（防 LIKE 兜底 %词% 误命中其他记忆）
_CONTENT = {
    "m1": "alpha", "m2": "beta", "m3": "gamma", "m4": "delta", "m5": "epsilon",
}


def _mem(conn, mid: str, priority: int = 50, status: str = "active") -> None:
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "ttl_days, created_at, updated_at, status, occurred_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mid, _CONTENT[mid], "episodic", priority, "dynamic", None,
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", status, "2026-01-01T00:00:00Z"),
    )


def _scene(conn, sid: str, status: str = "active") -> None:
    conn.execute(
        "INSERT INTO scenes (scene_id, title, content, heat, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (sid, f"scene-{sid}", "c", 1, status, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )


def _scene_mem(conn, sid: str, mid: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO scene_memories (scene_id, memory_id) VALUES (?,?)",
        (sid, mid),
    )


def _build_graph_db(tmp_path) -> sqlite3.Connection:
    """m1/m2/m3 同场景共现（belongs_to 边），m4 独立、m5 已归档。"""
    conn = _conn(tmp_path)
    for m in ("m1", "m2", "m3", "m4"):
        _mem(conn, m)
    _mem(conn, "m5", status="archived")
    _scene(conn, "s1")
    _scene_mem(conn, "s1", "m1")
    _scene_mem(conn, "s1", "m2")
    _scene_mem(conn, "s1", "m3")
    conn.commit()
    edge_dao.backfill_system_edges(conn, top_n=8, min_weight=1)
    return conn


def _search(conn, query: str, *, graph_enabled: bool, weight: float = 0.5,
            limit: int = 10) -> list[dict]:
    cfg = {
        "search": {
            "vector": {"enabled": False},
            "graph": {"enabled": graph_enabled, "weight": weight},
        }
    }
    return search_mod.search_memories(
        conn, None, query=query, limit=limit, include_sources=False, cfg=cfg,
    )


# ---------- _graph_candidates 单测 ----------

def test_graph_candidates_seed_exclusion_and_increment(tmp_path):
    conn = _build_graph_db(tmp_path)
    # seed = {m1}（bm25）+ {m2}（vec）→ 邻居 m1↔m3、m2↔m3 → m3 是增量
    bm25 = [{"memory_id": "m1"}]
    vec = [{"memory_id": "m2"}]
    cands = search_mod._graph_candidates(
        conn, bm25, vec, {"search": {"graph": {"enabled": True}}})
    ids = [c["memory_id"] for c in cands]
    assert "m3" in ids          # 增量邻居进候选
    assert "m1" not in ids      # 种子（bm25）排除
    assert "m2" not in ids      # 种子（vec）排除
    assert all(c["score"] > 0 for c in cands)
    db_mod.close(conn)


def test_graph_candidates_active_only(tmp_path):
    conn = _build_graph_db(tmp_path)
    # m5 已归档：给它也建边（人工插一条），确认不作为候选
    edge_dao.create_edge(conn, "m1", "m5", "belongs_to", weight=1.0, source="system")
    conn.commit()
    cands = search_mod._graph_candidates(
        conn, [{"memory_id": "m1"}], [], {"search": {"graph": {"enabled": True}}})
    ids = [c["memory_id"] for c in cands]
    assert "m2" in ids and "m3" in ids
    assert "m5" not in ids
    db_mod.close(conn)


def test_graph_candidates_disabled_and_no_edges(tmp_path):
    conn = _build_graph_db(tmp_path)
    # 关闭 → 空
    assert search_mod._graph_candidates(
        conn, [{"memory_id": "m1"}], [], {"search": {"graph": {"enabled": False}}}) == []
    # 无边（清空 memory_edges）→ 空
    edge_dao.delete_edges_by_source(conn, "system")
    conn.commit()
    assert search_mod._graph_candidates(
        conn, [{"memory_id": "m1"}], [], {"search": {"graph": {"enabled": True}}}) == []
    # 无 memory_edges 表 → 空（降级不抛）
    conn.execute("DROP TABLE memory_edges")
    conn.commit()
    assert search_mod._graph_candidates(
        conn, [{"memory_id": "m1"}], [], {"search": {"graph": {"enabled": True}}}) == []
    db_mod.close(conn)


# ---------- search_memories 集成 ----------

def test_search_graph_on_adds_neighbors(tmp_path):
    conn = _build_graph_db(tmp_path)
    res = _search(conn, "alpha", graph_enabled=True)
    ids = [r["memory_id"] for r in res]
    assert ids[0] == "m1"                 # 直接命中仍第一
    assert set(ids[:3]) == {"m1", "m2", "m3"}  # 邻居进入结果
    assert all("graph" in r["routes"] for r in res)
    db_mod.close(conn)


def test_search_graph_off_equals_plain_bm25(tmp_path):
    conn = _build_graph_db(tmp_path)
    res = _search(conn, "alpha", graph_enabled=False)
    assert [r["memory_id"] for r in res] == ["m1"]
    assert all("graph" not in r["routes"] for r in res)
    db_mod.close(conn)


def test_search_graph_no_edges_equals_off(tmp_path):
    conn = _build_graph_db(tmp_path)
    edge_dao.delete_edges_by_source(conn, "system")
    conn.commit()
    on = _search(conn, "alpha", graph_enabled=True)
    off = _search(conn, "alpha", graph_enabled=False)
    assert [r["memory_id"] for r in on] == [r["memory_id"] for r in off]
    assert on == off
    db_mod.close(conn)


def test_search_graph_weight_affects_ranking(tmp_path):
    conn = _build_graph_db(tmp_path)
    # 高权重 → 邻居反超直接命中
    res = _search(conn, "alpha", graph_enabled=True, weight=8.0)
    assert res[0]["memory_id"] in ("m2", "m3")
    db_mod.close(conn)


def test_search_graph_limit_respected(tmp_path):
    conn = _build_graph_db(tmp_path)
    res = _search(conn, "alpha", graph_enabled=True, limit=2)
    assert len(res) <= 2
    db_mod.close(conn)


# ---------- rrf graph 参数 ----------

def test_rrf_merge_graph_weight(tmp_path):
    bm25 = [{"memory_id": "A", "content": "a"}]
    vec = []
    graph = [
        {"memory_id": "B", "content": "b"},
        {"memory_id": "C", "content": "c"},
    ]
    # weight=1.0：B（rank0）= 1/61，C（rank1）= 1/62
    merged = rrf_mod.rrf_merge(bm25, vec, k=60, graph_results=graph, graph_weight=1.0)
    by_id = {r["memory_id"]: r for r in merged}
    assert by_id["B"]["score"] == pytest.approx(1 / 61)
    assert by_id["C"]["score"] == pytest.approx(1 / 62)
    assert "graph" in by_id["B"]["sources"]
    # weight=0.5 减半
    merged2 = rrf_mod.rrf_merge(bm25, vec, k=60, graph_results=graph, graph_weight=0.5)
    by_id2 = {r["memory_id"]: r for r in merged2}
    assert by_id2["B"]["score"] == pytest.approx(0.5 / 61)
    # graph_results=None → 两路等价（B/C 不出现）
    merged3 = rrf_mod.rrf_merge(bm25, vec, k=60)
    assert "B" not in {r["memory_id"] for r in merged3}


def test_rrf_merge_graph_rank_offset(tmp_path):
    bm25 = [{"memory_id": "A", "content": "a"}, {"memory_id": "D", "content": "d"}]
    graph = [{"memory_id": "B", "content": "b"}]
    # offset=2：B 的 rank 从 2 起算 → score = 1/(60+2+1) = 1/63，低于 bm25 rank1(1/62)
    merged = rrf_mod.rrf_merge(bm25, [], k=60, graph_results=graph,
                               graph_weight=1.0, graph_rank_offset=2)
    by_id = {r["memory_id"]: r for r in merged}
    assert by_id["A"]["score"] == pytest.approx(1 / 61)
    assert by_id["D"]["score"] == pytest.approx(1 / 62)
    assert by_id["B"]["score"] == pytest.approx(1 / 63)
    assert [r["memory_id"] for r in merged][:2] == ["A", "D"]


def test_search_fill_only_no_displacement(tmp_path):
    """fill_only=True：bm25 密集时图邻居只填空位、不挤占直接命中 → 与图关逐字节一致。"""
    conn = _build_graph_db(tmp_path)
    cfg_off = {"search": {"vector": {"enabled": False}, "graph": {"enabled": False}}}
    cfg_fill = {"search": {"vector": {"enabled": False},
                           "graph": {"enabled": True, "weight": 1.0, "fill_only": True}}}
    cfg_compete = {"search": {"vector": {"enabled": False},
                              "graph": {"enabled": True, "weight": 1.0, "fill_only": False}}}
    off = search_mod.search_memories(conn, None, query="alpha", limit=10,
                                     include_sources=False, cfg=cfg_off)
    fill = search_mod.search_memories(conn, None, query="alpha", limit=10,
                                      include_sources=False, cfg=cfg_fill)
    # 单跳查询 bm25 密集（alpha 只命中 m1）→ fill-only 下邻居填空位（limit=10 有空位）
    # 直接命中 m1 仍第一；邻居可出现在后面（fill），但不得挤掉 m1
    assert fill[0]["memory_id"] == "m1"
    assert [r["memory_id"] for r in off] == ["m1"]
    assert "graph" in fill[0]["routes"]
    # limit=1 时 fill-only 也绝不挤占：结果仍只有 m1
    fill1 = search_mod.search_memories(conn, None, query="alpha", limit=1,
                                       include_sources=False, cfg=cfg_fill)
    assert [r["memory_id"] for r in fill1] == ["m1"]
    # 竞争模式 limit=1：邻居可能挤掉 m1（权重 1.0 同 rank0 平权）
    compete1 = search_mod.search_memories(conn, None, query="alpha", limit=1,
                                          include_sources=False, cfg=cfg_compete)
    assert [r["memory_id"] for r in compete1] == ["m1"]  # 稳定排序保住 bm25 rank0
    db_mod.close(conn)


# ---------- T-137 图召回 v2：关系过滤 / 关系加权 ----------

def _build_v2_db(tmp_path) -> sqlite3.Connection:
    """m1 seed；m2=similar（语义边）、m3=belongs_to（共现）、m4=contradicts（否定边）。"""
    conn = _conn(tmp_path)
    for m in ("m1", "m2", "m3", "m4"):
        _mem(conn, m)
    conn.commit()
    edge_dao.create_edge(conn, "m1", "m2", "similar", weight=0.85, source="l1_conflict")
    edge_dao.create_edge(conn, "m1", "m3", "belongs_to", weight=3.0, source="system")
    edge_dao.create_edge(conn, "m1", "m4", "contradicts", weight=0.9, source="l1_conflict")
    conn.commit()
    return conn


def test_neighbors_exclude_relations(tmp_path):
    conn = _build_v2_db(tmp_path)
    # 默认（v1 行为）：4 个邻居全返回
    all_n = edge_dao.neighbors(conn, "m1")
    assert {n["memory_id"] for n in all_n} == {"m2", "m3", "m4"}
    # v2：排除 contradicts
    n2 = edge_dao.neighbors(conn, "m1", exclude_relations=["contradicts"])
    assert {n["memory_id"] for n in n2} == {"m2", "m3"}
    # 向后兼容：None/空 → 与 v1 等价
    assert edge_dao.neighbors(conn, "m1", exclude_relations=None) == all_n
    db_mod.close(conn)


def test_neighbors_relation_weights(tmp_path):
    conn = _build_v2_db(tmp_path)
    # 无加权：belongs_to weight=3.0 最大
    raw = edge_dao.neighbors(conn, "m1")
    assert raw[0]["memory_id"] == "m3"
    # v2 加权：belongs_to×0.3=0.9 < similar 0.85？0.9>0.85 → m3 仍第一，但差距缩小
    w = edge_dao.neighbors(conn, "m1", relation_weights={"belongs_to": 0.3})
    by_id = {n["memory_id"]: n["weight"] for n in w}
    assert abs(by_id["m3"] - 0.9) < 1e-6      # 3.0 × 0.3
    assert abs(by_id["m2"] - 0.85) < 1e-6     # 语义边不缩放
    # belongs_to×0.2=0.6 → similar 0.85 反超共现（m4 contradicts 0.9 未加权自然居首）
    w2 = edge_dao.neighbors(conn, "m1", relation_weights={"belongs_to": 0.2})
    rank = {n["memory_id"]: i for i, n in enumerate(w2)}
    assert rank["m2"] < rank["m3"]
    db_mod.close(conn)


def test_graph_candidates_v2_excludes_contradicts(tmp_path):
    conn = _build_v2_db(tmp_path)
    cands = search_mod._graph_candidates(
        conn, [{"memory_id": "m1"}], [], {"search": {"graph": {"enabled": True}}})
    ids = [c["memory_id"] for c in cands]
    assert "m2" in ids and "m3" in ids
    assert "m4" not in ids          # contradicts 排除（v2 默认）
    db_mod.close(conn)


def test_graph_candidates_v2_relation_weights_affect_rank(tmp_path):
    conn = _build_v2_db(tmp_path)
    # 无加权：m3（belongs_to 3.0）得分 3.0 > m2（0.85）
    cands1 = search_mod._graph_candidates(
        conn, [{"memory_id": "m1"}], [], {"search": {"graph": {"enabled": True, "relation_weights": None}}})
    assert cands1[0]["memory_id"] == "m3"
    # v2 默认加权：belongs_to×0.3=0.9 > 0.85 → m3 仍第一（验证配置可调）
    cands2 = search_mod._graph_candidates(
        conn, [{"memory_id": "m1"}], [], {"search": {"graph": {"enabled": True}}})
    assert cands2[0]["memory_id"] == "m3"
    # belongs_to×0.2 → similar 反超
    cands3 = search_mod._graph_candidates(
        conn, [{"memory_id": "m1"}], [], {"search": {"graph": {"enabled": True, "relation_weights": {"belongs_to": 0.2}}}})
    assert cands3[0]["memory_id"] == "m2"
    db_mod.close(conn)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
