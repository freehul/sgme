"""T-135 测试：语义边（搭 l1_conflict 顺风车，零新增调用）。

覆盖：
- relations 容错解析（正常 / 脏输入 / 旧格式兼容）
- _write_semantic_edges 过滤（conf 阈值 / 归档候选 / 非 active / 配置开关）
- resolve_conflicts 端到端挂接（落库后写边 + semantic_edges_written 计数 + 幂等）
"""

from __future__ import annotations

import json

import httpx
import pytest

from sgme import config
from sgme.engine import l15
from sgme.data import db as db_mod, edge_dao, memory_dao


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _insert_existing(mem_conn, content, dim_ids, **kw):
    return memory_dao.insert_memory(
        mem_conn, content=content,
        memory_type=kw.get("memory_type", "persona"),
        priority=kw.get("priority", 60),
        time_velocity=kw.get("time_velocity", "static"),
        ttl_days=kw.get("ttl_days"),
        dimension_ids=dim_ids,
        created_at=kw.get("created_at", "2026-01-01T00:00:00Z"),
        updated_at=kw.get("updated_at", "2026-01-01T00:00:00Z"),
    )


def _mock_llm_client(response_body: str) -> httpx.Client:
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


# ---------- relations 解析 ----------

def test_parse_relations_valid():
    raw = [
        {"candidate_id": "c1", "relation": "similar", "confidence": 0.85},
        {"candidate_id": "c2", "relation": "causes", "confidence": 0.9},
        {"candidate_id": "c3", "relation": "contradicts", "confidence": 0.7},
    ]
    rels = l15._parse_relations(raw)
    assert len(rels) == 3
    assert rels[0] == l15.RelationEdge("c1", "similar", 0.85)
    assert rels[1].relation == "causes"
    assert rels[2].confidence == 0.7


def test_parse_relations_tolerant():
    """脏输入容错：非 list / 字段缺失 / 非法 relation / confidence 越界 / 非 dict 项。"""
    assert l15._parse_relations(None) == []
    assert l15._parse_relations("not-a-list") == []
    assert l15._parse_relations([{"candidate_id": "c1"}]) == []            # 缺 relation
    assert l15._parse_relations([{"relation": "similar", "confidence": 0.8}]) == []  # 缺 candidate
    assert l15._parse_relations([{"candidate_id": "c1", "relation": "friends_with", "confidence": 0.8}]) == []  # 非法 relation
    assert l15._parse_relations([{"candidate_id": "c1", "relation": "similar", "confidence": "abc"}]) == []
    rels = l15._parse_relations([{"candidate_id": "c1", "relation": "similar", "confidence": 1.7}])
    assert rels[0].confidence == 1.0                                        # 越界钳制
    rels = l15._parse_relations([{"candidate_id": "c1", "relation": "similar", "confidence": -0.3}])
    assert rels[0].confidence == 0.0
    assert l15._parse_relations([42, "x"]) == []                            # 非 dict 项跳过


def test_parse_l15_output_with_relations_and_legacy():
    text = json.dumps([
        {"new_memory_index": 0, "candidate_ids": ["c1"], "action": "merge",
         "merged_content": "m", "reason": "r",
         "relations": [{"candidate_id": "c2", "relation": "similar", "confidence": 0.8}]},
        {"new_memory_index": 1, "candidate_ids": [], "action": "store", "reason": "r"},  # 旧格式无 relations
    ])
    decisions = l15.parse_l15_output(text)
    assert len(decisions) == 2
    assert decisions[0].relations[0].candidate_id == "c2"
    assert decisions[0].relations[0].relation == "similar"
    assert decisions[1].relations == []   # 旧格式兼容


# ---------- _write_semantic_edges 过滤 ----------

def test_write_edges_writes_valid(mem_conn, cfg):
    cand = _insert_existing(mem_conn, "旧记忆：用户喜欢打飞盘", ["tech_stack"])
    decision = l15.ConflictDecision(
        new_memory_index=0, candidate_ids=[], action="store",
        relations=[l15.RelationEdge(cand, "similar", 0.85)],
    )
    n = l15._write_semantic_edges(mem_conn, {"memory_id": "new1"}, decision, cfg)
    assert n == 1
    assert edge_dao.count_edges(mem_conn, source="l1_conflict") == 1
    edges = edge_dao.list_edges(mem_conn, source="l1_conflict")
    assert edges[0]["from_id"] == "new1" and edges[0]["to_id"] == cand
    assert edges[0]["relation"] == "similar"
    assert abs(edges[0]["weight"] - 0.85) < 1e-6


def test_write_edges_below_min_weight_skipped(mem_conn, cfg):
    cand = _insert_existing(mem_conn, "旧记忆", ["tech_stack"])
    decision = l15.ConflictDecision(
        0, [], "store",
        relations=[l15.RelationEdge(cand, "similar", 0.4)],  # < 0.6 默认阈值
    )
    assert l15._write_semantic_edges(mem_conn, {"memory_id": "new1"}, decision, cfg) == 0
    assert edge_dao.count_edges(mem_conn) == 0


def test_write_edges_skips_archived_candidates(mem_conn, cfg):
    """被 update/merge 归档的候选跳过（替代关系由 archive 链承载）。"""
    cand = _insert_existing(mem_conn, "旧记忆", ["tech_stack"])
    decision = l15.ConflictDecision(
        0, [cand], "update",  # candidate_ids 命中 → 归档 → relations 里的它跳过
        relations=[l15.RelationEdge(cand, "similar", 0.95)],
    )
    assert l15._write_semantic_edges(mem_conn, {"memory_id": "new1"}, decision, cfg) == 0
    assert edge_dao.count_edges(mem_conn) == 0


def test_write_edges_skips_inactive_candidate(mem_conn, cfg):
    cand = _insert_existing(mem_conn, "旧记忆", ["tech_stack"])
    memory_dao.reject_memory(mem_conn, cand, "测试 reject")
    decision = l15.ConflictDecision(
        0, [], "store",
        relations=[l15.RelationEdge(cand, "similar", 0.9)],
    )
    assert l15._write_semantic_edges(mem_conn, {"memory_id": "new1"}, decision, cfg) == 0
    assert edge_dao.count_edges(mem_conn) == 0


def test_write_edges_disabled_by_config(mem_conn, cfg):
    cand = _insert_existing(mem_conn, "旧记忆", ["tech_stack"])
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["l15"]["semantic_edges"]["enabled"] = False
    decision = l15.ConflictDecision(
        0, [], "store",
        relations=[l15.RelationEdge(cand, "similar", 0.95)],
    )
    assert l15._write_semantic_edges(mem_conn, {"memory_id": "new1"}, decision, cfg2) == 0


def test_write_edges_no_memory_id_or_no_relations(mem_conn, cfg):
    cand = _insert_existing(mem_conn, "旧记忆", ["tech_stack"])
    # 无 memory_id（skip 决策路径）
    d1 = l15.ConflictDecision(0, [], "skip", relations=[l15.RelationEdge(cand, "similar", 0.9)])
    assert l15._write_semantic_edges(mem_conn, {}, d1, cfg) == 0
    # 无 relations
    d2 = l15.ConflictDecision(0, [], "store")
    assert l15._write_semantic_edges(mem_conn, {"memory_id": "new1"}, d2, cfg) == 0


# ---------- resolve_conflicts 端到端挂接 ----------

def test_resolve_conflicts_writes_semantic_edges(monkeypatch, mem_conn, cfg):
    """LLM 输出带 relations 的 store 裁决 → 落库后写边 + 计数。"""
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 10000)
    cand1 = _insert_existing(mem_conn, "旧记忆：经常去公园打飞盘", ["tech_stack"])
    cand2 = _insert_existing(mem_conn, "旧记忆：喜欢喝咖啡", ["tech_stack"])
    body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [], "action": "store", "reason": "r",
        "relations": [
            {"candidate_id": cand1, "relation": "similar", "confidence": 0.85},
            {"candidate_id": cand2, "relation": "similar", "confidence": 0.3},  # 低于阈值
        ],
    }])
    cli = _mock_llm_client(body)
    new_memories = [{
        "content": "新记忆：上周末和朋友去公园玩飞盘", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.stored) == 1
    assert result.semantic_edges_written == 1
    edges = edge_dao.list_edges(mem_conn, source="l1_conflict")
    assert len(edges) == 1
    assert edges[0]["from_id"] == result.stored[0]
    assert edges[0]["to_id"] == cand1
    assert edges[0]["relation"] == "similar"


def test_resolve_conflicts_update_archives_no_semantic_edge(monkeypatch, mem_conn, cfg):
    """update 命中候选 → 候选归档 → relations 里的它不写边（archive 链承载替代关系）。"""
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 10000)
    cand = _insert_existing(mem_conn, "旧记忆：喜欢喝咖啡", ["tech_stack"])
    body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [cand], "action": "update", "reason": "r",
        "relations": [{"candidate_id": cand, "relation": "similar", "confidence": 0.95}],
    }])
    cli = _mock_llm_client(body)
    new_memories = [{
        "content": "新记忆：更喜欢喝美式咖啡", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.updated) == 1
    assert result.semantic_edges_written == 0
    assert edge_dao.count_edges(mem_conn) == 0


def test_resolve_conflicts_multiple_memories_first_with_edges(monkeypatch, mem_conn, cfg):
    """回归：多条新记忆且首条带语义边——_write_semantic_edges 隐式事务必须提交，
    否则第二条 insert_memory 的 BEGIN 抛 "cannot start a transaction within a transaction"。"""
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 10000)
    cand = _insert_existing(mem_conn, "旧记忆：飞盘俱乐部", ["tech_stack"])
    body = json.dumps([
        {
            "new_memory_index": 0, "candidate_ids": [], "action": "store", "reason": "r",
            "relations": [{"candidate_id": cand, "relation": "similar", "confidence": 0.85}],
        },
        {"new_memory_index": 1, "candidate_ids": [], "action": "store", "reason": "r"},
    ])
    cli = _mock_llm_client(body)
    new_memories = [
        {"content": "新记忆1：玩飞盘", "dimension_ids": ["tech_stack"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
        {"content": "新记忆2：喝咖啡", "dimension_ids": ["tech_stack"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
    ]
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.stored) == 2          # 第二条不崩、正常落库
    assert result.semantic_edges_written == 1
    assert edge_dao.count_edges(mem_conn, source="l1_conflict") == 1


def test_resolve_conflicts_rerun_idempotent(monkeypatch, mem_conn, cfg):
    """确定性 edge_id 幂等：同一裁决重跑不产生重复边。"""
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 10000)
    cand = _insert_existing(mem_conn, "旧记忆：打飞盘", ["tech_stack"])
    body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [], "action": "store", "reason": "r",
        "relations": [{"candidate_id": cand, "relation": "similar", "confidence": 0.8}],
    }])
    cli = _mock_llm_client(body)
    new_memories = [{
        "content": "新记忆：飞盘俱乐部训练", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    # 第一次：store 新记忆 + 写 1 边（带 source_ref 供幂等比对）
    r1 = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli, source_ref="f1.txt")
    assert r1.semantic_edges_written == 1
    # 第二次：同源同内容幂等跳过（不落库、不写边）
    r2 = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli, source_ref="f1.txt")
    assert r2.semantic_edges_written == 0
    assert edge_dao.count_edges(mem_conn, source="l1_conflict") == 1
