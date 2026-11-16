"""ST-58/T-295（B3a+B3b 成对）：装箱估算校准与并集口径。

依据 docs/plans/SGME-提炼管线降本增效-实施清单-v1.0.md 批次 4：
- B3a 常量标定：FORMAT_CHARS_PER_CANDIDATE 120→162（实测候均 162.3）、
  CHARS_PER_TOKEN 2.0→1.85（实测隐含 1.86）、TEMPLATE_OVERHEAD_TOKENS 1500→800（实测 799）；
- B3b 估算改**增量并集口径**（实际 prompt 是批内并集去重，旧「各组之和」高估 1.3~1.8 倍）；
- 兜底断言：实际 usage.prompt_tokens > batch_budget×1.05 → anomaly_warn；
- 一键回退开关：l3_adjudicate.packing_estimate_mode = union（默认）/ sum（旧口径）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from sgme import config
from sgme.engine import l3_adjudicate
from sgme.data import db as db_mod, memory_dao


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture(autouse=True)
def _vector_enabled(monkeypatch):
    from sgme.data.search import vector as vector_mod

    monkeypatch.setattr(
        vector_mod, "embed", lambda text, cfg, client=None: [1.0, 0.0, 0.0, 0.0]
    )


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _mock_llm_client(decision_body: str, usage: dict | None = None) -> httpx.Client:
    payload: dict = {"choices": [{"message": {"content": decision_body}}]}
    if usage is not None:
        payload["usage"] = usage

    def handler(req):
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


# ---------- B3a：常量标定 ----------

def test_b3a_constants_calibrated():
    """常量须为实测标定值（120→162 / 2.0→1.85 / 1500→800），回退即缺陷。"""
    assert l3_adjudicate.FORMAT_CHARS_PER_CANDIDATE == 162
    assert l3_adjudicate.CHARS_PER_TOKEN == 1.85
    assert l3_adjudicate.TEMPLATE_OVERHEAD_TOKENS == 800


# ---------- B3b：并集口径 vs 旧 sum 口径 ----------

def _shared_group(idx: int, shared_id: str, shared_len: int = 8000):
    """构造引用同一条共享候选的分组（三组共享 → sum 口径重复计费三次）。"""
    return l3_adjudicate.CandidateGroup(
        new_memory_index=idx,
        new_memory={"content": f"NM{idx}", "dimension_ids": ["tech_stack"]},
        candidates=[{"memory_id": shared_id, "content": "x" * shared_len}],
    )


def test_union_mode_counts_shared_candidate_once():
    """三组共享同一大候选：union 口径一批装下；sum 口径（回退开关）拆成三批。"""
    groups = [_shared_group(0, "shared"), _shared_group(1, "shared"), _shared_group(2, "shared")]
    # union 三组并集 = 候选 8000+162 + 3 条新记忆各 163 字符 = 8651 字符
    # → ceil(8651/1.85) = 4677 tokens；预算恰好容纳三组一批
    budget = l3_adjudicate.TEMPLATE_OVERHEAD_TOKENS + 4677
    batches = l3_adjudicate.build_batches(groups, budget)  # 默认 union
    assert len(batches) == 1, f"并集口径应一批装下，实际 {len(batches)} 批"
    assert [c["memory_id"] for c in batches[0].candidates] == ["shared"]  # 并集去重

    legacy = l3_adjudicate.build_batches(groups, budget, estimate_mode="sum")
    assert len(legacy) == 3, f"旧 sum 口径（回退开关）应拆三批，实际 {len(legacy)} 批"


def test_estimate_mode_config_validation():
    """packing_estimate_mode 只认 union/sum，非法值退回默认 union。"""
    merged = config._merge_l3_adjudicate_config({"packing_estimate_mode": "bogus"})
    assert merged["packing_estimate_mode"] == "union"
    merged = config._merge_l3_adjudicate_config({"packing_estimate_mode": "sum"})
    assert merged["packing_estimate_mode"] == "sum"
    merged = config._merge_l3_adjudicate_config(None)
    assert merged["packing_estimate_mode"] == "union"


# ---------- B3 兜底断言：实际 usage 超预算 5% → anomaly_warn ----------

def _setup_one_batch(mem_conn, cfg):
    """构造恰好一批送检的最小场景（一条候选 + 一条新记忆）。"""
    cfg["l3_adjudicate"] = {"prescreen": None}
    old_id = memory_dao.insert_memory(
        mem_conn, content="旧事实：项目使用 Python 3.10",
        memory_type="persona", priority=60, time_velocity="static",
        ttl_days=None, dimension_ids=["tech_stack"],
    )
    new_memories = [{"content": "新事实：项目升级到 Python 3.11",
                     "dimension_ids": ["tech_stack"], "memory_type": "persona",
                     "priority": 80, "time_velocity": "static"}]
    return old_id, new_memories


def test_overbudget_prompt_tokens_warns(mem_conn, cfg):
    """实际 prompt_tokens 超批预算 5% → anomaly_warn=True（装箱估算失准信号）。"""
    old_id, new_memories = _setup_one_batch(mem_conn, cfg)
    body = json.dumps([
        {"new_memory_index": 0, "candidate_ids": [old_id], "action": "store", "reason": "ok"},
    ])
    huge_usage = {"prompt_tokens": 10_000_000, "completion_tokens": 10, "total_tokens": 10_000_010}
    result = l3_adjudicate.resolve_conflicts(
        new_memories, mem_conn, cfg,
        client=_mock_llm_client(body, usage=huge_usage),
    )
    assert result.anomaly_warn is True


def test_within_budget_no_warn(mem_conn, cfg):
    """实际 usage 在预算内 → 不告警（无信号不产噪音）。"""
    old_id, new_memories = _setup_one_batch(mem_conn, cfg)
    body = json.dumps([
        {"new_memory_index": 0, "candidate_ids": [old_id], "action": "store", "reason": "ok"},
    ])
    ok_usage = {"prompt_tokens": 500, "completion_tokens": 10, "total_tokens": 510}
    result = l3_adjudicate.resolve_conflicts(
        new_memories, mem_conn, cfg,
        client=_mock_llm_client(body, usage=ok_usage),
    )
    assert result.anomaly_warn is False
