# -*- coding: utf-8 -*-
"""T-140 测试：多 Agent scope（灰度隔离）。

覆盖：_filter_agent_scope（NULL 全通/default 共享/同 agent 可见/异 agent 不可见）、
search 集成（默认关全通、开+agent_id 过滤）、写侧 agent_tag 填充（_resolve_file_agent）、
persist_memories 打标。
"""

from __future__ import annotations

import sqlite3

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, session_dao
from sgme.data import search as search_mod
from sgme.engine import pipeline


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def conn(tmp_path, cfg) -> sqlite3.Connection:
    c = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(c, cfg["dimensions"], cfg["aliases"])
    yield c
    db_mod.close(c)


def _ins(conn, content, agent_tag=None):
    return memory_dao.insert_memory(
        conn, content=content, memory_type="persona", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["goals"],
        agent_tag=agent_tag,
    )


# ---------- _filter_agent_scope ----------

def test_filter_scope_visibility(conn):
    _ins(conn, "alpha 无主记忆")                    # NULL → 全通
    _ins(conn, "alpha 共享记忆", agent_tag="default")  # default → 全通
    _ins(conn, "alpha 我的记忆", agent_tag="agent-a")  # 同 agent 可见
    _ins(conn, "alpha 别的记忆", agent_tag="agent-b")  # 异 agent 不可见

    results = [
        {"memory_id": mid, "content": c}
        for mid, c in [
            (r["memory_id"], r["content"]) for r in conn.execute(
                "SELECT memory_id, content FROM memories").fetchall()
        ]
    ]
    kept = search_mod._filter_agent_scope(conn, results, agent_id="agent-a")
    contents = {r["content"] for r in kept}
    assert "alpha 无主记忆" in contents        # NULL 全通
    assert "alpha 共享记忆" in contents        # default 全通
    assert "alpha 我的记忆" in contents        # 同 agent
    assert "alpha 别的记忆" not in contents    # 异 agent 隔离


def test_filter_scope_agent_none_sees_only_shared(conn):
    _ins(conn, "alpha 无主记忆")
    _ins(conn, "alpha 别的记忆", agent_tag="agent-b")
    results = [
        {"memory_id": r["memory_id"], "content": r["content"]}
        for r in conn.execute("SELECT memory_id, content FROM memories").fetchall()
    ]
    kept = search_mod._filter_agent_scope(conn, results, agent_id=None)
    contents = {r["content"] for r in kept}
    assert "alpha 无主记忆" in contents
    assert "alpha 别的记忆" not in contents


def test_filter_scope_empty_input(conn):
    assert search_mod._filter_agent_scope(conn, [], agent_id="a") == []


# ---------- search 集成 ----------

def test_search_agent_scope_default_off(conn):
    """默认关：agent_tag 任意都返回（灰度全通，行为不变）。"""
    _ins(conn, "alpha 我的", agent_tag="agent-a")
    _ins(conn, "alpha 你的", agent_tag="agent-b")
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False,
        cfg={"search": {"vector": {"enabled": False}}},
    )
    assert len(res) == 2


def test_search_agent_scope_enabled(conn):
    _ins(conn, "alpha 我的", agent_tag="agent-a")
    _ins(conn, "alpha 你的", agent_tag="agent-b")
    _ins(conn, "alpha 无主")
    cfg = {
        "search": {"vector": {"enabled": False}},
        "agent_scope": {"enabled": True},
    }
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False, cfg=cfg,
        agent_id="agent-a",
    )
    contents = {r["content"] for r in res}
    assert "alpha 我的" in contents
    assert "alpha 无主" in contents
    assert "alpha 你的" not in contents


# ---------- 写侧 agent_tag ----------

def test_resolve_file_agent(tmp_path):
    session_conn = db_mod.connect_session(tmp_path)
    session_dao.insert_raw_file(
        session_conn, file_id="f1", path="x/1.jsonl", session_key="s1",
        started_at="2026-01-01T00:00:00Z", agent_id="agent-hermes",
        ended_at="2026-01-01T00:00:01Z", status="new", size=100,
    )
    assert pipeline._resolve_file_agent(session_conn, "f1") == "agent-hermes"
    assert pipeline._resolve_file_agent(session_conn, "no-such") is None
    assert pipeline._resolve_file_agent(None, "f1") is None
    session_conn.close()


def test_persist_memories_tags_agent(monkeypatch, tmp_path, cfg):
    """写侧：persist_memories(agent_tag=...) 给无主记忆打标；已带 tag 保留。"""
    from sgme.engine import refine as refine_mod
    mem_conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    result = refine_mod.RefineResult(
        file_id="f1",
        memories=[
            {"content": "记忆A", "dimensions": ["goals"], "memory_type": "persona",
             "priority": 60, "time_velocity": "static", "source_message_ids": [0]},
            {"content": "记忆B", "dimensions": ["goals"], "memory_type": "persona",
             "priority": 60, "time_velocity": "static", "source_message_ids": [1],
             "agent_tag": "agent-explicit"},  # 显式携带 → 保留
        ],
        status="ok",
    )
    # mock L1.5：resolve_conflicts 直接 store（短路 LLM 依赖）
    from sgme.engine import l15 as l15_mod

    def _fake_resolve(new_memories, mem_conn, cfg, **kw):
        res = l15_mod.L15Result()
        for i, m in enumerate(new_memories):
            mid = memory_dao.insert_memory(
                mem_conn, content=m["content"], memory_type=m.get("memory_type", "persona"),
                priority=m.get("priority", 50), time_velocity=m.get("time_velocity", "static"),
                ttl_days=None, dimension_ids=m.get("dimension_ids", m.get("dimensions", [])),
                agent_tag=m.get("agent_tag"),
            )
            m["memory_id"] = mid
            res.stored.append(mid)
        return res

    monkeypatch.setattr("sgme.engine.pipeline.l15_mod.resolve_conflicts", _fake_resolve)
    monkeypatch.setattr("sgme.engine.pipeline.refine_mod.finalize_refinement", lambda *a, **k: None)

    stats = pipeline.persist_memories(result, mem_conn, cfg, agent_tag="agent-hermes")
    assert stats["stored"] == 2
    rows = {r["content"]: r["agent_tag"] for r in mem_conn.execute(
        "SELECT content, agent_tag FROM memories").fetchall()}
    assert rows["记忆A"] == "agent-hermes"   # 打标
    assert rows["记忆B"] == "agent-explicit"  # 显式保留
    mem_conn.close()
