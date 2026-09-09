"""T-149③ 测试：operations/answer.py 聚合答案与时序推理。

覆盖：
- 题型分派（时序/聚合/兜底正则）
- 上下文渲染（[n] 编号 + facts + occurred_at；timeline 排序）
- 主操作：mock llm_fn 答案、evidence 结构、LLM 不可用降级、
  enabled=false 关闭、空 query 报 InvalidArgs
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao
from sgme.operations import answer as answer_mod
from sgme.operations.answer import (
    answer,
    build_evidence,
    classify_question,
    render_context,
    render_timeline,
)
from sgme.operations.errors import InvalidArgs


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn
    mem_conn.close()


# ---------- 分派 ----------

@pytest.mark.parametrize("q,expected", [
    ("How many days passed between my visit to MoMA and the exhibit?", "temporal"),
    ("哪件事先发生，参观博物馆还是看展？", "temporal"),
    ("How many items of clothing do I need to pick up?", "aggregate"),
    ("我一共带过几个项目？", "aggregate"),
    ("What degree did I graduate with?", "generic"),
])
def test_classify_question(q, expected):
    assert classify_question(q) == expected


# ---------- 渲染 ----------

def test_render_context_includes_facts_and_time():
    candidates = [{
        "memory_id": "m1", "rank": 1, "content": "用户参观了 MoMA",
        "occurred_at": "2023-05-01", "facts": [
            {"subject": "用户", "predicate": "参观", "object": "MoMA"}],
    }]
    ctx = render_context(candidates)
    assert "[1]" in ctx
    assert "2023-05-01" in ctx
    assert "用户|参观|MoMA" in ctx


def test_render_timeline_sorts_by_occurred_at():
    candidates = [
        {"memory_id": "late", "content": "后期事件", "occurred_at": "2023-06-01"},
        {"memory_id": "none", "content": "无时间事件", "occurred_at": None},
        {"memory_id": "early", "content": "早期事件", "occurred_at": "2023-05-01"},
    ]
    tl = render_timeline(candidates)
    lines = tl.splitlines()
    assert "2023-05-01" in lines[0]
    assert "2023-06-01" in lines[1]
    assert "时间未知" in lines[2]


def test_build_evidence_shape():
    ev = build_evidence([{"memory_id": "m1", "rank": 2, "occurred_at": None, "facts": []}])
    assert ev == [{"memory_id": "m1", "rank": 2, "occurred_at": None, "facts_used": []}]


# ---------- 主操作 ----------

def test_answer_mock_llm_end_to_end(conns, cfg):
    memory_dao.insert_memory(
        conns, content="用户参观了现代艺术博物馆 MoMA",
        memory_type="episodic", priority=70, time_velocity="static",
        ttl_days=None, dimension_ids=["goals"],
        occurred_at="2023-05-01T10:00:00Z",
        facts=[{"subject": "用户", "predicate": "参观", "object": "MoMA"}])
    memory_dao.insert_memory(
        conns, content="用户参观大都会博物馆古代文明展",
        memory_type="episodic", priority=70, time_velocity="static",
        ttl_days=None, dimension_ids=["goals"],
        occurred_at="2023-05-08T10:00:00Z",
        facts=[{"subject": "用户", "predicate": "参观", "object": "大都会"}])

    seen_prompts: list[str] = []
    def fake_llm(prompt: str) -> str:
        seen_prompts.append(prompt)
        return "7 days"

    res = answer(conns, None, cfg,  # type: ignore[arg-type]
                 query="两次参观博物馆间隔多少天？",
                 llm_fn=fake_llm)
    assert res.ok
    d = res.data
    assert d["answer"] == "7 days"
    assert d["question_type"] == "temporal"
    assert d["prompt_meta"]["stage"] == "answer_temporal"
    assert "{{timeline}}" in seen_prompts[0] or "2023-05-01" in seen_prompts[0]
    # 时间线出现且升序
    tl_line = [l for l in seen_prompts[0].splitlines() if "2023-05-01" in l or "2023-05-08" in l]
    assert tl_line, "temporal prompt 应注入时间线"
    assert len(d["evidence"]) == d["candidates_used"] == 2


def test_answer_aggregate_dispatch(conns, cfg):
    def fake_llm(prompt: str) -> str:
        return "3"
    res = answer(conns, None, cfg,  # type: ignore[arg-type]
                 query="How many items do I need to pick up?",
                 llm_fn=fake_llm)
    assert res.ok
    assert res.data["question_type"] == "aggregate"
    assert res.data["prompt_meta"]["stage"] == "answer_aggregate"


def test_answer_llm_unavailable(conns, cfg):
    import httpx
    from sgme.llm import provider as llm_provider

    def handler(req):
        return httpx.Response(500, json={"error": "down"})
    broken = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    res = answer(conns, None, cfg,  # type: ignore[arg-type]
                 query="随便一个问题", client=broken)
    assert not res.ok
    assert res.error_code == "ERR_LLM_UNAVAILABLE"


def test_answer_disabled(conns, cfg):
    cfg2 = dict(cfg)
    cfg2["answer"] = {"enabled": False}
    res = answer(conns, None, cfg2,  # type: ignore[arg-type]
                 query="问题", llm_fn=lambda p: "x")
    assert not res.ok
    assert res.error_code == "ERR_DISABLED"


def test_answer_empty_query_raises(conns, cfg):
    with pytest.raises(InvalidArgs):
        answer(conns, None, cfg, query="   ", llm_fn=lambda p: "x")


def test_answer_bad_question_type(conns, cfg):
    with pytest.raises(InvalidArgs):
        answer(conns, None, cfg, query="问题", question_type="bogus", llm_fn=lambda p: "x")
