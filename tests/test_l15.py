"""T5 测试：L1.5 冲突提炼（四动作 + 归档链 + 候选池 + 超预算）。

mock LLM 返回固定四动作裁决。
"""

from __future__ import annotations

import json

import httpx
import pytest

from sgme import config
from sgme.engine import l15
from sgme.data import db as db_mod, memory_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _mock_llm_client(response_body: str) -> httpx.Client:
    """构造 mock httpx 客户端，返回固定 L1.5 裁决。"""
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_llm_client_sequence(bodies: list[str]) -> httpx.Client:
    state = {"i": 0}
    def handler(req):
        i = state["i"]
        state["i"] = i + 1
        body = bodies[min(i, len(bodies) - 1)]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_llm_client_capture(captured: list[str], decision_body: str | None = None) -> httpx.Client:
    """mock 客户端：记录每次请求的 prompt 文本；按批内新记忆数生成 store 裁决（或固定 body）。"""
    def handler(req):
        payload = json.loads(req.content)
        prompt = payload["messages"][0]["content"]
        captured.append(prompt)
        if decision_body is not None:
            body = decision_body
        else:
            n = prompt.count("[新记忆#")
            body = json.dumps([
                {"new_memory_index": i, "candidate_ids": [], "action": "store"}
                for i in range(n)
            ])
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _insert_existing(mem_conn, content, dim_ids, **kw):
    """插入一条已存在的旧记忆，返回 memory_id。"""
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


# ---------- render_l15 ----------

def test_render_l15_replaces_placeholders(cfg):
    prompt = l15.render_l15(
        [{"content": "新事实", "dimension_ids": ["tech_stack"], "memory_type": "persona",
          "priority": 80, "time_velocity": "static"}],
        [{"memory_id": "c1", "content": "旧事实", "tags": ["tech_stack"],
          "memory_type": "persona", "priority": 60, "updated_at": "2026-01-01T00:00:00Z"}],
    )
    assert "{{new_memories}}" not in prompt
    assert "{{candidates}}" not in prompt
    assert "新事实" in prompt
    assert "旧事实" in prompt


# ---------- parse_l15_output ----------

def test_parse_l15_output_valid():
    text = json.dumps([
        {"new_memory_index": 0, "candidate_ids": ["c1"], "action": "update",
         "merged_content": "合并", "reason": "更具体"},
    ])
    decisions = l15.parse_l15_output(text)
    assert len(decisions) == 1
    assert decisions[0].action == "update"
    assert decisions[0].candidate_ids == ["c1"]


def test_parse_l15_output_bad_action_defaults_store():
    text = json.dumps([
        {"new_memory_index": 0, "candidate_ids": [], "action": "invalid"},
    ])
    decisions = l15.parse_l15_output(text)
    assert decisions[0].action == "store"


def test_parse_l15_output_markdown_block():
    text = '```json\n[{"new_memory_index":0,"candidate_ids":[],"action":"store"}]\n```'
    decisions = l15.parse_l15_output(text)
    assert len(decisions) == 1


def test_parse_l15_output_bad_json_raises():
    with pytest.raises(l15.L15Error):
        l15.parse_l15_output("not json")


# ---------- 候选池 ----------

def test_candidate_pool_or_semantics(mem_conn):
    """候选池 OR：任一新记忆维度命中即召回。"""
    _insert_existing(mem_conn, "旧 A", ["tech_stack"])
    _insert_existing(mem_conn, "旧 B", ["identity"])
    _insert_existing(mem_conn, "旧 C", ["status"])
    new_memories = [
        {"content": "新", "dimension_ids": ["tech_stack", "identity"]},
    ]
    candidates, warn = l15.build_candidate_pool(mem_conn, new_memories)
    contents = {c["content"] for c in candidates}
    # tech_stack 或 identity 维度的旧记忆都应召回（OR）
    assert "旧 A" in contents
    assert "旧 B" in contents
    # status 维度不在新记忆维度中，不召回
    assert "旧 C" not in contents
    assert warn is False


def test_candidate_pool_empty_when_no_shared_dim(mem_conn):
    """新记忆维度无任何旧记忆 → 候选池空。"""
    _insert_existing(mem_conn, "旧", ["tech_stack"])
    new_memories = [{"content": "新", "dimension_ids": ["status"]}]
    candidates, _ = l15.build_candidate_pool(mem_conn, new_memories)
    assert candidates == []


def test_candidate_pool_topk_truncation_warns(mem_conn):
    """候选池不再硬截断 top_k；小池全量返回，超 char_budget 才截断。"""
    # 插入 60 条同维度旧记忆
    for i in range(60):
        _insert_existing(mem_conn, f"旧 {i}", ["tech_stack"])
    new_memories = [{"content": "新", "dimension_ids": ["tech_stack"]}]
    candidates, warn = l15.build_candidate_pool(mem_conn, new_memories)
    # 60 条短内容不超 char_budget → 全量返回，无 warn
    assert len(candidates) == 60
    assert warn is False


def test_candidate_pool_1000_candidates_truncation(mem_conn):
    """超预算 edge：长内容候选超 char_budget → 截断 + anomaly_warn（不崩溃）。"""
    long_content = "x" * 200  # 每条 ~200 字符
    for i in range(200):
        _insert_existing(mem_conn, f"{long_content} {i}", ["tech_stack"])
    new_memories = [{"content": "新", "dimension_ids": ["tech_stack"]}]
    candidates, warn = l15.build_candidate_pool(mem_conn, new_memories)
    # 200 × 200 = 40K > 24K char_budget → 截断，warn=True
    assert len(candidates) < 200
    assert warn is True


# ---------- T-18 候选池治理：不整池截断 + 单记忆预算 top-k（铁律 #7） ----------

def test_candidate_pool_no_whole_pool_truncation_across_groups(mem_conn):
    """T-18：多条新记忆各自候选之和超预算，但单记忆不超 → 整池不截断。

    旧 B2 实现按整池 char_budget 截断（可能漏冲突）；铁律 #7 仅允许单记忆候选超预算截断。
    """
    dims = ["tech_stack", "identity", "status", "family", "projects"]
    long = "x" * 200
    for dim in dims:
        for i in range(60):
            _insert_existing(mem_conn, f"{long} {dim}-{i}", [dim])
    new_memories = [{"content": f"新-{d}", "dimension_ids": [d]} for d in dims]
    candidates, warn = l15.build_candidate_pool(mem_conn, new_memories)
    # 5×60=300 条共 ~61K 字符 > 24000，但每组 60 条 ~12K 字符 < 24000 → 全量召回无 warn
    assert len(candidates) == 300
    assert warn is False


def test_candidate_pool_per_memory_topk_truncation_warns(mem_conn):
    """T-18：单记忆候选超预算 → 按 priority 降序 top-k 截断 + anomaly_warn。"""
    for i in range(100):
        _insert_existing(mem_conn, f"{'y' * 500}-{i:03d}", ["tech_stack"], priority=100 - i)
    new_memories = [{"content": "新", "dimension_ids": ["tech_stack"]}]
    candidates, warn = l15.build_candidate_pool(mem_conn, new_memories)
    assert warn is True
    assert len(candidates) == l15.DEFAULT_TOP_K
    # top-k 保留 priority 最高的 50 条（100..51）
    kept = {c["content"] for c in candidates}
    assert kept == {f"{'y' * 500}-{i:03d}" for i in range(50)}
    assert all(c["priority"] >= 51 for c in candidates)


# ---------- T-18 分批：按上下文预算贪心装箱（铁律 #7） ----------

def _mk_group(idx: int, n_cands: int, content_len: int) -> l15.CandidateGroup:
    """构造候选分组（不落库）：新记忆 + n_cands 条指定长度候选。"""
    return l15.CandidateGroup(
        new_memory_index=idx,
        new_memory={"content": f"NM{idx}", "dimension_ids": ["tech_stack"]},
        candidates=[{"memory_id": f"c{idx}_{j}", "content": "x" * content_len}
                    for j in range(n_cands)],
    )


def test_build_batches_packs_groups_within_budget():
    """T-18：贪心装箱——同一新记忆只进一批、每批不超预算、全部候选均参与比对。"""
    g0, g1, g2 = _mk_group(0, 2, 3000), _mk_group(1, 2, 3000), _mk_group(2, 2, 3000)
    c0, c1, c2 = (l15.estimate_group_tokens(g) for g in (g0, g1, g2))
    # 预算 = 模板开销 + 前两组估算，恰好容纳前两组 → 2 批
    budget = l15.TEMPLATE_OVERHEAD_TOKENS + c0 + c1
    batches = l15.build_batches([g0, g1, g2], budget)
    assert len(batches) == 2
    assert [b.start_index for b in batches] == [0, 2]
    assert [m["content"] for m in batches[0].new_memories] == ["NM0", "NM1"]
    assert [m["content"] for m in batches[1].new_memories] == ["NM2"]
    # 全部候选均参与比对（并集无遗漏、无重复）
    all_ids = [c["memory_id"] for b in batches for c in b.candidates]
    assert len(all_ids) == 6 and len(set(all_ids)) == 6
    # 每批估算成本（含模板开销）不超过预算
    for b in batches:
        cost = l15.TEMPLATE_OVERHEAD_TOKENS + sum(
            l15.estimate_group_tokens(g) for g in (g0, g1, g2)
            if any(m is g.new_memory for m in b.new_memories)
        )
        assert cost <= budget


def test_build_batches_boundary_exact_fit_and_overflow():
    """T-18：批次边界——恰好等于预算放入一批，超 1 token 拆批；单组超预算独立成批不丢弃。"""
    g0, g1 = _mk_group(0, 1, 3000), _mk_group(1, 1, 3000)
    c0, c1 = l15.estimate_group_tokens(g0), l15.estimate_group_tokens(g1)
    overhead = l15.TEMPLATE_OVERHEAD_TOKENS
    # 恰好放下（模板开销 + 两组）→ 一批
    assert len(l15.build_batches([g0, g1], overhead + c0 + c1)) == 1
    # 少 1 token → 拆成两批
    assert len(l15.build_batches([g0, g1], overhead + c0 + c1 - 1)) == 2
    # 单组自身超预算 → 独立成批不丢弃
    ghuge = _mk_group(2, 1, 100000)
    batches = l15.build_batches([ghuge], 100)
    assert len(batches) == 1
    assert batches[0].candidates[0]["memory_id"] == "c2_0"
    assert batches[0].start_index == 2


# ---------- 四动作落库 ----------

def test_action_store_inserts_new_memory(mem_conn, cfg):
    """store：INSERT 新记忆，无归档。"""
    _insert_existing(mem_conn, "无关旧记忆", ["identity"])
    new_memories = [{
        "content": "全新事实", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.stored) == 1
    assert result.archived == []
    mem = memory_dao.get_memory(mem_conn, result.stored[0])
    assert mem["content"] == "全新事实"
    assert "tech_stack" in mem["tags"]


def test_action_skip_no_change(mem_conn, cfg):
    """skip：不动，不写入。"""
    _insert_existing(mem_conn, "旧", ["tech_stack"])
    new_memories = [{
        "content": "重复信息", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": ["x"], "action": "skip"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert result.skipped == [0]
    assert result.stored == []
    assert result.updated == []
    assert result.merged == []


def test_action_update_archives_old(mem_conn, cfg):
    """update：归档候选行 + INSERT 新记忆。

    checklist: 旧行出现在 memory_archive 且 superseded_by=新行 id。
    """
    old_id = _insert_existing(mem_conn, "旧技术栈：Python 3.10", ["tech_stack"])
    new_memories = [{
        "content": "新技术栈：Python 3.11", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 85, "time_velocity": "static",
    }]
    body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [old_id],
        "action": "update", "reason": "版本更新",
    }])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.updated) == 1
    new_id = result.updated[0]
    # 旧行已归档
    assert old_id in result.archived
    archive = memory_dao.find_by_superseded_by(mem_conn, new_id)
    assert len(archive) == 1
    assert archive[0]["memory_id"] == old_id
    assert archive[0]["superseded_by"] == new_id
    assert archive[0]["content"] == "旧技术栈：Python 3.10"
    # memories 表已无旧行
    assert memory_dao.get_memory(mem_conn, old_id) is None
    # 新行存在
    new_mem = memory_dao.get_memory(mem_conn, new_id)
    assert new_mem["content"] == "新技术栈：Python 3.11"


def test_action_merge_combines_and_archives(mem_conn, cfg):
    """merge：合并行 + 归档所有命中候选。

    checklist: 时间戳并集存在（merged 记忆 updated_at 覆盖双方）。
    """
    old1 = _insert_existing(
        mem_conn, "用户用 Python", ["tech_stack"],
        updated_at="2026-01-01T00:00:00Z",
    )
    old2 = _insert_existing(
        mem_conn, "用户写 SGME 项目", ["tech_stack", "projects"],
        updated_at="2026-02-01T00:00:00Z",
    )
    new_memories = [{
        "content": "用户用 Python 写 SGME 项目（补充）", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 85, "time_velocity": "static",
        "updated_at": "2026-03-01T00:00:00Z",
    }]
    body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [old1, old2],
        "action": "merge", "merged_content": "用户用 Python 写 SGME 项目",
        "reason": "互补片段合并",
    }])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.merged) == 1
    new_id = result.merged[0]
    # 两个候选都归档
    assert old1 in result.archived
    assert old2 in result.archived
    # 合并行内容
    merged = memory_dao.get_memory(mem_conn, new_id)
    assert merged["content"] == "用户用 Python 写 SGME 项目"
    # 时间戳并集：取候选 + 新记忆 updated_at 的最大值
    assert merged["updated_at"] == "2026-03-01T00:00:00Z"
    # 旧候选都已归档
    assert memory_dao.get_memory(mem_conn, old1) is None
    assert memory_dao.get_memory(mem_conn, old2) is None


def test_action_update_no_candidate_falls_back_to_store(mem_conn, cfg):
    """update 但无候选 id → 退化为 store。"""
    new_memories = [{
        "content": "新事实", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [],
        "action": "update", "reason": "无候选",
    }])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.stored) == 1
    assert result.updated == []


def test_action_merge_no_merged_content_falls_back_to_store(mem_conn, cfg):
    """merge 但无 merged_content → 退化为 store。"""
    old = _insert_existing(mem_conn, "旧", ["tech_stack"])
    new_memories = [{
        "content": "新", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [old],
        "action": "merge", "merged_content": None,
    }])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.stored) == 1
    assert result.merged == []


# ---------- TTL 回填 ----------

def test_ttl_backfill_from_dimension_default(mem_conn, cfg):
    """新记忆 ttl_days=None → 按维度默认回填（status → 7 天）。"""
    new_memories = [{
        "content": "用户当前状态：累", "dimension_ids": ["status"],
        "memory_type": "persona", "priority": 60, "time_velocity": "dynamic",
        "ttl_days": None,
    }]
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    mem = memory_dao.get_memory(mem_conn, result.stored[0])
    assert mem["ttl_days"] == 7  # status 维度默认 TTL


def test_ttl_keeps_explicit_value(mem_conn, cfg):
    """新记忆显式 ttl_days=14 → 保留。"""
    new_memories = [{
        "content": "x", "dimension_ids": ["status"],
        "memory_type": "persona", "priority": 60, "time_velocity": "dynamic",
        "ttl_days": 14,
    }]
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    mem = memory_dao.get_memory(mem_conn, result.stored[0])
    assert mem["ttl_days"] == 14


def test_ttl_static_dimension_remains_null(mem_conn, cfg):
    """静态维度 → ttl_days=None。"""
    new_memories = [{
        "content": "用户身份", "dimension_ids": ["identity"],
        "memory_type": "persona", "priority": 90, "time_velocity": "static",
        "ttl_days": None,
    }]
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    mem = memory_dao.get_memory(mem_conn, result.stored[0])
    assert mem["ttl_days"] is None


def test_ttl_ideas_forces_null_over_other_dimensions(mem_conn, cfg):
    """创意池铁律（2026-08-13）：含 ideas 维度 → ttl 强制 None（长期保存）。

    ideas + projects（projects 默认 90d）共存时，必须 None——否则创意 90 天后
    过期退出注入，违背创意池「ideas + ttl_days=NULL」定义。
    """
    new_memories = [{
        "content": "用户想做一个个人知识管理工具（创意）",
        "dimension_ids": ["ideas", "projects"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
        "ttl_days": None,
    }]
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    mem = memory_dao.get_memory(mem_conn, result.stored[0])
    assert mem["ttl_days"] is None  # ideas 覆盖 projects 的 90d


def test_ttl_ideas_forces_null_even_with_explicit_value(mem_conn, cfg):
    """含 ideas 维度时，显式 ttl_days 也被覆盖为 None（创意长期保存是硬规则）。"""
    new_memories = [{
        "content": "用户灵感：做记忆可视化（创意）",
        "dimension_ids": ["ideas"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
        "ttl_days": 30,
    }]
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    mem = memory_dao.get_memory(mem_conn, result.stored[0])
    assert mem["ttl_days"] is None


# ---------- 跨批不重复 ----------

def test_same_new_memory_only_in_one_batch(mem_conn, cfg):
    """同一新记忆只进一批：即使候选分批，新记忆只裁决一次。"""
    # 插入足够多候选触发分批
    for i in range(100):
        _insert_existing(mem_conn, f"旧 {i}", ["tech_stack"])
    new_memories = [{
        "content": "新事实", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    # 两批候选，每批都返回 store（如果跨批重复裁决，会插入 2 条）
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    # 只插入 1 条（同一新记忆只进一批/只裁决一次）
    assert len(result.stored) == 1


# ---------- T-18 万级候选池：分批送检不漏冲突 ----------

def test_resolve_conflicts_multi_batch_full_recall_no_dup(monkeypatch, mem_conn, cfg):
    """T-18：超大候选池分批后全部候选均参与比对，同一新记忆只进一批，不整池截断。"""
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 10000)
    captured: list[str] = []
    cli = _mock_llm_client_capture(captured)
    inserted: list[str] = []
    # tech_stack 组小（~1.7K tokens），identity/status 组大（~4.9K tokens）
    for dim, n in (("tech_stack", 10), ("identity", 30), ("status", 30)):
        for i in range(n):
            inserted.append(_insert_existing(mem_conn, f"{'x' * 200} {dim}-{i}", [dim]))
    new_memories = [
        {"content": f"NM-{d}", "dimension_ids": [d],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"}
        for d in ("tech_stack", "identity", "status")
    ]
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    # 3 组 ~71K 字符总候选 > 旧 24000 整池预算；新语义下不整池截断、无 anomaly_warn
    assert result.anomaly_warn is False
    # 按上下文预算分批：tech_stack+identity 一批，status 一批（组不拆散）
    assert len(captured) == 2
    # 全部 70 条候选均参与比对（每条至少出现在某批 prompt）
    for mid in inserted:
        assert any(f"[候选#{mid}]" in p for p in captured), f"候选 {mid} 未参与比对"
    # 同一新记忆只进一批（内容恰出现一次）
    for m in new_memories:
        assert sum(p.count(f"content: {m['content']}") for p in captured) == 1
    # 落库完整
    assert len(result.stored) == 3


def test_resolve_conflicts_decision_index_remap(monkeypatch, mem_conn, cfg):
    """T-18：批内裁决索引重映射回全局——第 2 批的 index 0 作用于全局第 2 条新记忆。"""
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 1000)
    for i in range(10):
        _insert_existing(mem_conn, f"{'x' * 200} 旧A {i}", ["tech_stack"])
    old1 = _insert_existing(mem_conn, f"{'x' * 200} 旧B", ["identity"])
    new_memories = [
        {"content": "NM0", "dimension_ids": ["tech_stack"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
        {"content": "NM1", "dimension_ids": ["identity"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
    ]
    # 两组各自超 1000 token 预算 → 各一批；第 1 批 skip（全局 0），第 2 批 update（批内 0 → 全局 1）
    bodies = [
        json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "skip"}]),
        json.dumps([{"new_memory_index": 0, "candidate_ids": [old1], "action": "update", "reason": "r"}]),
    ]
    cli = _mock_llm_client_sequence(bodies)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert result.skipped == [0]
    assert len(result.updated) == 1
    assert result.archived == [old1]
    assert result.stored == []
    new_mem = memory_dao.get_memory(mem_conn, result.updated[0])
    assert new_mem["content"] == "NM1"


def test_resolve_conflicts_candidate_less_memory_skips_llm(mem_conn, cfg):
    """T-18：无候选的新记忆不进 LLM 批 → 落库阶段默认 store（短路到单记忆粒度）。"""
    _insert_existing(mem_conn, "旧 tech", ["tech_stack"])
    captured: list[str] = []
    cli = _mock_llm_client_capture(captured)
    new_memories = [
        {"content": "NM0", "dimension_ids": ["tech_stack"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
        {"content": "NM1", "dimension_ids": ["family"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
    ]
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert len(result.stored) == 2
    # 只调一次 LLM，且 prompt 中只有有候选的 NM0
    assert len(captured) == 1
    assert "content: NM0" in captured[0]
    assert "content: NM1" not in captured[0]


def test_resolve_conflicts_per_memory_truncation_warns(monkeypatch, mem_conn, cfg):
    """T-18：单记忆候选超上下文预算 → top-k 截断 + anomaly_warn（保留告警语义）。"""
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 500)
    for i in range(60):
        _insert_existing(mem_conn, f"{'x' * 200} {i}", ["tech_stack"])
    new_memories = [{
        "content": "新", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    captured: list[str] = []
    cli = _mock_llm_client_capture(captured)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert result.anomaly_warn is True
    assert len(result.stored) == 1
    # 截断后候选 = top-50（60 → 50）；候选区从最后一个「# 输入：候选记忆」标题起计数
    assert len(captured) == 1
    cand_section = captured[0].rsplit("# 输入：候选记忆", 1)[1]
    assert cand_section.count("[候选#") == l15.DEFAULT_TOP_K


# ---------- 空输入 ----------

def test_resolve_conflicts_empty_input(mem_conn, cfg):
    """空新记忆列表 → 空结果。"""
    result = l15.resolve_conflicts([], mem_conn, cfg)
    assert result.stored == []
    assert result.skipped == []


def test_resolve_conflicts_no_candidates_short_circuit(mem_conn, cfg):
    """候选池为空 → 全部 store（短路，不调 LLM，不报错）。"""
    new_memories = [{
        "content": "全新事实", "dimension_ids": ["identity"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }, {
        "content": "另一个全新事实", "dimension_ids": ["family"],
        "memory_type": "persona", "priority": 70, "time_velocity": "static",
    }]
    # LLM 故意全挂：若短路生效则不触发 LLM 调用 → 不报错
    def handler(req):
        raise httpx.ConnectError("connection refused")
    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert result.error is None
    assert len(result.stored) == 2
    assert result.stored == [m["memory_id"] for m in new_memories]


# ---------- LLM 失败 ----------

def test_resolve_conflicts_llm_unavailable_marks_error(mem_conn, cfg):
    """LLM 全链失败 → error + anomaly_warn（候选池非空时才调 LLM）。"""
    # 先插入候选，确保候选池非空 → 走 LLM 裁决路径
    _insert_existing(mem_conn, "旧记忆", ["tech_stack"])

    def handler(req):
        raise httpx.ConnectError("connection refused")
    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    new_memories = [{
        "content": "x", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    assert result.error is not None
    assert result.anomaly_warn is True
    assert result.stored == []


def test_resolve_conflicts_bad_json_falls_back_to_store(mem_conn, cfg):
    """L1.5 输出解析失败 → 默认 store（保守不丢数据）。"""
    _insert_existing(mem_conn, "旧", ["tech_stack"])
    new_memories = [{
        "content": "新", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    cli = _mock_llm_client("not json at all")
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, client=cli)
    # 默认 store
    assert len(result.stored) == 1


# ---------- #33 版本观测 ----------

def test_resolve_conflicts_records_refine_run_and_prompt_version(mem_conn, cfg):
    """#33：逐批记 refine_run（version/variant/action 分布）+ prompt_version 透传落库。"""
    from sgme.data.refine_dao import RefineRunRecorder
    _insert_existing(mem_conn, "旧", ["tech_stack"])
    new_memories = [{
        "content": "新事实", "dimension_ids": ["tech_stack"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    body = json.dumps([{"new_memory_index": 0, "candidate_ids": [], "action": "store"}])
    cli = _mock_llm_client(body)
    result = l15.resolve_conflicts(
        new_memories, mem_conn, cfg, client=cli, prompt_version="l1_extraction:v002",
    )
    # prompt_meta 透传
    assert result.prompt_meta is not None
    assert result.prompt_meta["stage"] == "l1_conflict"
    assert result.prompt_meta["version"].startswith("working-")
    # refine_run 已记录
    runs = RefineRunRecorder.list_by_stage(mem_conn, "l1_conflict")
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["version"].startswith("working-")
    import json as _json
    assert _json.loads(runs[0]["action_counts"]) == {"store": 1}
    # prompt_version 写入 memories
    mem = memory_dao.get_memory(mem_conn, result.stored[0])
    assert mem["prompt_version"] == "l1_extraction:v002"


def test_resolve_conflicts_short_circuit_writes_prompt_version(mem_conn, cfg):
    """#33：候选池空短路 store 也透传 prompt_version（无 refine_run，未调 LLM）。"""
    from sgme.data.refine_dao import RefineRunRecorder
    new_memories = [{
        "content": "全新事实", "dimension_ids": ["identity"],
        "memory_type": "persona", "priority": 80, "time_velocity": "static",
    }]
    result = l15.resolve_conflicts(new_memories, mem_conn, cfg, prompt_version="l1_extraction:v001")
    assert len(result.stored) == 1
    mem = memory_dao.get_memory(mem_conn, result.stored[0])
    assert mem["prompt_version"] == "l1_extraction:v001"
    assert RefineRunRecorder.list_by_stage(mem_conn, "l1_conflict") == []
