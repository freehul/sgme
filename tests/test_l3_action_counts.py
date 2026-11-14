"""T1（三缺口补齐 2026-11-14）：L1.5 批路径 refine_runs.action_counts 记账。

依据 docs/plans/任务清单-三缺口补齐-20261114.md T1：
- 批路径 ``_finish_run`` 按裁决决策统计各 action 计数（仓库 c6a8c7c 已引入，
  此处回归锁定——生产全 ``{}`` 是 docker cp 快照落后所致，部署由 lead 统一处理）；
- 统计是旁路：异常只记日志，绝不打断裁决主流程；
- 幂等跳过（同 source_ref 同内容已存在）不进批、批路径数不到，单独计
  ``idempotent_skipped``（同 prescreen/empty_pool 的独立 run 范式）。
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
    """与 test_l3_adjudicate.py 同款：mock embed，消除隐性网络依赖。"""
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


def _mock_llm_client(decision_body: str) -> httpx.Client:
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": decision_body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _insert_existing(mem_conn, content, dim_ids):
    return memory_dao.insert_memory(
        mem_conn, content=content,
        memory_type="persona", priority=60, time_velocity="static",
        ttl_days=None, dimension_ids=dim_ids,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
    )


def _batch_runs(mem_conn):
    """批路径 run（排除同型独立记录：prescreen_skip / near_dup / empty_pool / idempotent_skip）。"""
    return mem_conn.execute(
        "SELECT run_id, provider, action_counts, status, error FROM refine_runs "
        "WHERE stage='l1_conflict' AND provider NOT IN "
        "('prescreen_skip', 'near_dup', 'empty_pool', 'idempotent_skip')",
    ).fetchall()


# ---------- 批路径 action_counts ----------

def test_batch_action_counts_recorded(mem_conn, cfg):
    """一批两条决策（update + skip）→ 批 run 的 action_counts 含各 action 计数。"""
    cfg["l3_adjudicate"] = {"prescreen": None}
    old_id = _insert_existing(mem_conn, "旧技术栈：Python 3.10", ["tech_stack"])
    new_memories = [
        {"content": "新技术栈：Python 3.11", "dimension_ids": ["tech_stack"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
        {"content": "重复信息：Python 3.10 相关", "dimension_ids": ["tech_stack"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
    ]
    body = json.dumps([
        {"new_memory_index": 0, "candidate_ids": [old_id], "action": "update", "reason": "版本更新"},
        {"new_memory_index": 1, "candidate_ids": [old_id], "action": "skip"},
    ])
    result = l3_adjudicate.resolve_conflicts(
        new_memories, mem_conn, cfg, client=_mock_llm_client(body),
    )
    runs = _batch_runs(mem_conn)
    assert len(runs) == 1, f"应恰好一条批 run，实际 {[dict(r) for r in runs]}"
    assert json.loads(runs[0]["action_counts"]) == {"update": 1, "skip": 1}
    assert runs[0]["status"] == "ok"
    assert len(result.updated) == 1


def test_empty_decisions_records_empty_counts_no_error(mem_conn, cfg):
    """空 decisions（LLM 返回 []）→ 不报错，批 run 记 action_counts={}（落库段默认 store）。"""
    cfg["l3_adjudicate"] = {"prescreen": None}
    _insert_existing(mem_conn, "旧事实甲", ["goals"])
    new_memories = [{"content": "新事实甲", "dimension_ids": ["goals"],
                     "memory_type": "persona", "priority": 50, "time_velocity": "static"}]
    result = l3_adjudicate.resolve_conflicts(
        new_memories, mem_conn, cfg, client=_mock_llm_client("[]"),
    )
    runs = _batch_runs(mem_conn)
    assert len(runs) == 1
    assert json.loads(runs[0]["action_counts"]) == {}
    assert runs[0]["status"] == "ok"
    # 空裁决 → 落库段默认 store（既有行为，回归确认）
    assert len(result.stored) == 1


# ---------- 幂等跳过独立记账 ----------

def test_idempotent_skip_recorded_as_separate_run(mem_conn, cfg):
    """幂等跳过不进批 → 单独一条 provider='idempotent_skip' run，action_counts={'idempotent_skipped': n}。"""
    cfg["l3_adjudicate"] = {"prescreen": None}
    memory_dao.insert_memory(
        mem_conn, content="X 已被 Y 替代",
        memory_type="persona", priority=60, time_velocity="static",
        ttl_days=None, dimension_ids=["goals"],
        sources=[("F:1", "session")],
    )
    new_memories = [{"content": "X 已被 Y 替代", "dimension_ids": ["goals"],
                     "memory_type": "persona", "priority": 50, "time_velocity": "static"}]
    res = l3_adjudicate.resolve_conflicts(
        new_memories, mem_conn, cfg, client=_mock_llm_client("[]"),
        source_ref="F:1",
    )
    assert res.stored == [] and 0 in res.skipped  # 幂等跳过照旧生效
    rows = mem_conn.execute(
        "SELECT provider, memories_count, action_counts, status FROM refine_runs "
        "WHERE stage='l1_conflict' AND provider='idempotent_skip'",
    ).fetchall()
    assert len(rows) == 1, f"应恰好一条幂等跳过 run，实际 {len(rows)}"
    assert rows[0]["memories_count"] == 1
    assert json.loads(rows[0]["action_counts"]) == {"idempotent_skipped": 1}
    assert rows[0]["status"] == "ok"


def test_no_idempotent_run_without_skip(mem_conn, cfg):
    """无幂等跳过 → 不产生 idempotent_skip run（无信号不产噪音，与同型范式一致）。"""
    cfg["l3_adjudicate"] = {"prescreen": None}
    new_memories = [{"content": "全新事实乙", "dimension_ids": ["goals"],
                     "memory_type": "persona", "priority": 50, "time_velocity": "static"}]
    l3_adjudicate.resolve_conflicts(
        new_memories, mem_conn, cfg, client=_mock_llm_client("[]"), source_ref=None,
    )
    rows = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM refine_runs WHERE provider='idempotent_skip'",
    ).fetchone()
    assert rows["c"] == 0


# ---------- 旁路防护 ----------

def test_counts_error_does_not_break_adjudication(mem_conn, cfg, monkeypatch):
    """统计异常（决策项 action 属性炸）只记日志回退空计数，裁决主流程照常完成。"""
    cfg["l3_adjudicate"] = {"prescreen": None}

    class _EvilDecision:
        """首次访问 action（统计时）即炸，之后恢复正常——模拟统计路径被污染，
        同时不污染落库路径（落库读 action 是主流程职责，不属旁路）。"""

        def __init__(self, candidate_ids):
            self.new_memory_index = 0
            self.candidate_ids = candidate_ids
            self.reason = "evil"
            self.relations: list = []
            self._calls = 0

        @property
        def action(self):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("evil action")
            return "update"

    old_id = _insert_existing(mem_conn, "旧事实丙", ["goals"])
    evil = _EvilDecision([old_id])
    monkeypatch.setattr(
        l3_adjudicate, "_parse_adjudication_with_repair",
        lambda *a, **kw: [evil],
    )
    new_memories = [{"content": "新事实丙", "dimension_ids": ["goals"],
                     "memory_type": "persona", "priority": 50, "time_velocity": "static"}]
    body = json.dumps([
        {"new_memory_index": 0, "candidate_ids": [old_id], "action": "update", "reason": "更新"},
    ])
    result = l3_adjudicate.resolve_conflicts(
        new_memories, mem_conn, cfg, client=_mock_llm_client(body),
    )
    assert len(result.updated) == 1, f"统计炸了主流程也不能断，实际 result={result}"
    runs = _batch_runs(mem_conn)
    assert len(runs) == 1 and runs[0]["status"] == "ok"
    assert json.loads(runs[0]["action_counts"]) == {}  # 回退空计数，不带毒
