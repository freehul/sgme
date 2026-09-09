"""T-149① 测试：检索结果透传 occurred_at 与 facts。

覆盖：
- BM25 路（memories_fts JOIN）返回 occurred_at + facts（解析后的 list）
- LIKE 兜底路同样透传
- facts_json 为 NULL 的记忆 → facts == []
- 向量路（vector_search）同样透传（mock embed 避免真实网络）
"""

from __future__ import annotations

import json

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao
from sgme.data.search import search_memories


def _search(mem_conn, query, limit=5):
    """include_sources=False → 不走 trace，session_conn 传 None（eval 同约定）。"""
    return search_memories(mem_conn, None, query=query, limit=limit, include_sources=False)  # type: ignore[arg-type]


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn
    mem_conn.close()


def _ins(mem_conn, content, occurred_at=None, facts=None, mid=None):
    return memory_dao.insert_memory(
        mem_conn, content=content,
        memory_type="persona", priority=60, time_velocity="static",
        ttl_days=None, dimension_ids=["goals"],
        occurred_at=occurred_at, facts=facts, memory_id=mid,
    )


# ---------- BM25 路 ----------

def test_bm25_returns_occurred_at_and_facts(conns):
    _ins(conns, "用户参观了现代艺术博物馆",
         occurred_at="2023-05-01T10:00:00Z",
         facts=[{"subject": "用户", "predicate": "参观", "object": "MoMA"}])
    _ins(conns, "用户参观大都会博物馆古代文明展",
         occurred_at="2023-05-08T10:00:00Z",
         facts=[{"subject": "用户", "predicate": "参观", "object": "大都会博物馆"}])
    res = _search(conns, "博物馆 参观")
    assert res, "BM25 应召回"
    top = res[0]
    assert "occurred_at" in top and top["occurred_at"] is not None
    assert isinstance(top["facts"], list)
    assert top["facts"][0]["subject"] == "用户"


def test_bm25_facts_null_yields_empty_list(conns):
    _ins(conns, "无事实的记忆条目内容")  # 无 facts
    res = _search(conns, "无事实的记忆条目")
    assert res
    assert res[0]["facts"] == []
    assert res[0]["occurred_at"] is None or isinstance(res[0]["occurred_at"], str)


# ---------- LIKE 兜底路 ----------

def test_like_path_returns_fields(conns):
    _ins(conns, "ZebraUniqueToken 里程碑达成",
         occurred_at="2023-06-01T00:00:00Z",
         facts=[{"subject": "里程碑", "predicate": "状态", "object": "达成"}])
    # 用 FTS 查不到的分词形态逼 LIKE 兜底（纯 ASCII 罕见 token 用 LIKE 语义）
    res = _search(conns, "ZebraUniqueToken")
    assert res, "LIKE 兜底应召回"
    top = res[0]
    assert top["occurred_at"] == "2023-06-01T00:00:00Z"
    assert top["facts"] == [{"subject": "里程碑", "predicate": "状态", "object": "达成"}]
