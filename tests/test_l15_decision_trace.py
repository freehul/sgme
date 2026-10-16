"""ST-58/T-296：`L15Result.decisions` 逐条决策留痕（G3-c 门禁的必要使能）。

背景：G3-c 要求「逐条比对 A/B 两臂的 L1.5 裁决」，而此前只有**批次级**
`action_counts` 聚合，无法定位「是哪条新记忆被判成了别的动作」。
本字段补齐该粒度，且**仅进程内返回、不落库**（无新表/新列、零迁移）。
"""

from __future__ import annotations

import json

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao
from sgme.engine import l15


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture(autouse=True)
def _vector_stub(monkeypatch):
    """mock embed（照 test_l15.py 先例，避免隐性网络依赖）。"""
    from sgme.data.search import vector as vector_mod

    monkeypatch.setattr(
        vector_mod, "embed", lambda text, cfg, client=None: [1.0, 0.0, 0.0, 0.0]
    )


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn


def _mock_llm(body: str):
    """固定返回体的 LLM 桩（照 test_l15.py 的 _mock_llm_client）。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _new(idx_content: str, dimension: str = "tech_stack") -> dict:
    return {
        "content": idx_content, "dimension_ids": [dimension],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }


def _insert_existing(conn, content: str, dims: list[str]) -> str:
    return memory_dao.insert_memory(
        conn, content=content, memory_type="persona", priority=50,
        time_velocity="static", ttl_days=None, dimension_ids=dims,
    )


def test_decisions_aligned_with_new_memories(mem_conn, cfg):
    """每条新记忆一个留痕项，下标与输入对齐（跨臂比对的键）。"""
    _insert_existing(mem_conn, "旧记忆", ["tech_stack"])
    new_memories = [_new("新记忆甲"), _new("新记忆乙")]
    body = json.dumps([
        {"new_memory_index": 0, "candidate_ids": [], "action": "store", "reason": "全新"},
        {"new_memory_index": 1, "candidate_ids": [], "action": "skip", "reason": "重复"},
    ])
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=_mock_llm(body))

    assert [d["new_memory_index"] for d in result.decisions] == [0, 1]
    assert [d["action"] for d in result.decisions] == ["store", "skip"]
    assert all(d["idempotent_skip"] is False for d in result.decisions)


def test_decisions_record_candidate_ids_and_reason(mem_conn, cfg):
    """命中候选的 id 与理由入留痕（G3-c 需要能看出「判给了谁」）。"""
    old_id = _insert_existing(mem_conn, "用户用 A 工具", ["tech_stack"])
    new_memories = [_new("用户用 A 工具和 B 工具")]
    body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [old_id], "action": "update",
        "merged_content": "用户用 A 工具和 B 工具", "reason": "同主体+增量",
    }])
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=_mock_llm(body))

    d0 = result.decisions[0]
    assert d0["action"] == "update"
    assert d0["candidate_ids"] == [old_id]
    assert "增量" in d0["reason"]


def test_decisions_fallback_store_recorded(mem_conn, cfg):
    """LLM 未给某条新记忆裁决 → 走「无裁决默认 store」，留痕如实记为 store。"""
    _insert_existing(mem_conn, "旧", ["tech_stack"])
    new_memories = [_new("甲"), _new("乙")]
    # 只给 index 0 的裁决，index 1 缺失
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "skip"}])
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=_mock_llm(body))

    by_idx = {d["new_memory_index"]: d for d in result.decisions}
    assert by_idx[0]["action"] == "skip"
    assert by_idx[1]["action"] == "store"  # 默认兜底
    assert "无裁决默认" in by_idx[1]["reason"]


def test_decisions_not_persisted_to_db(mem_conn, cfg):
    """留痕**不落库**：refine_runs 无新列（零迁移约束）。"""
    _insert_existing(mem_conn, "旧", ["tech_stack"])
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    l15.resolve_conflicts([_new("甲")], mem_conn, cfg, client=_mock_llm(body))

    cols = [r[1] for r in mem_conn.execute("PRAGMA table_info(refine_runs)").fetchall()]
    assert "decisions" not in cols
    # 批次级聚合仍在（向后兼容）
    row = mem_conn.execute(
        "SELECT action_counts FROM refine_runs WHERE stage='l1_conflict' "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None and json.loads(row[0]) == {"store": 1}


def test_empty_pool_short_circuit_records_decisions(mem_conn, cfg):
    """候选池为空 → 全部 store 短路，同样要有逐条留痕（A/B 比对不能因短路而丢失样本）。"""
    body = json.dumps([])  # 不应被调用
    result = l15.resolve_conflicts([_new("甲", "family")], mem_conn, cfg,
                                   client=_mock_llm(body))
    assert len(result.decisions) == 1
    assert result.decisions[0]["action"] == "store"
    assert result.decisions[0]["new_memory_index"] == 0
