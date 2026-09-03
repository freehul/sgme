"""L1.5 候选池向量预筛（prescreen）测试：维度候选截断 + 向量 Top-K 并集 + 降级回退。

2026-08-12 成本治理：候选池全量召回在记忆库 9k+ 时单次 l1_conflict 消耗 67-100 万 tokens。
预筛开启后：维度 OR 候选截断到 dimension_top_n（priority 降序）+ 向量 Top-K 并集，
单记忆候选 ≤ vector_top_k + dimension_top_n，prompt 从 ~100 万 tokens 降到 ~2 万。
embed 不可达 / 向量检索异常 → 自动回退全量召回（宁贵勿漏，行为与现状完全一致）。
"""

from __future__ import annotations

import json

import pytest

from sgme import config
from sgme.engine import l15
from sgme.data import db as db_mod, memory_dao
from sgme.data.search import vector as vector_mod


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _insert_existing(mem_conn, content, dim_ids, priority=60, memory_id=None):
    """插入一条旧记忆，返回 memory_id。"""
    return memory_dao.insert_memory(
        mem_conn, content=content,
        memory_type="persona", priority=priority,
        time_velocity="static", ttl_days=None,
        dimension_ids=dim_ids,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def _new_mem(content="新记忆内容", dims=("tech_stack",)):
    return {"content": content, "dimension_ids": list(dims),
            "memory_type": "persona", "priority": 80, "time_velocity": "static"}


# ---------- 预筛配置 ----------

def _ps_cfg(enabled=True, vector_top_k=50, dimension_top_n=50):
    return {"enabled": enabled, "vector_top_k": vector_top_k, "dimension_top_n": dimension_top_n}


# ---------- 0. 配置透传 ----------

def test_load_config_exposes_l15_prescreen():
    """load_config 组装结果含 l15.prescreen（T-25：sgme.yaml 透传 + 默认兜底）。"""
    from sgme import config as sgme_config
    # SGME_HOME 隔离下默认路径已重定向到 tmp，显式指包内真实配置验证透传
    cfg = sgme_config.load_config(
        sgme_path=sgme_config.RESOURCE_ROOT / "config" / "sgme.yaml"
    )
    l15_cfg = cfg.get("l15", {})
    assert "prescreen" in l15_cfg
    ps = l15_cfg["prescreen"]
    # 默认结构完整（生产 sgme.yaml 显式 enabled=true 覆盖默认 false）
    assert "enabled" in ps
    assert ps["vector_top_k"] >= 1
    assert ps["dimension_top_n"] >= 1
    # 生产配置文件显式开启
    assert ps["enabled"] is True


# ---------- 1. 维度候选截断 ----------

def test_prescreen_limits_dimension_candidates(mem_conn, cfg, monkeypatch):
    """启用预筛 + 向量检索为空 → 维度候选截断到 dimension_top_n，不再全量召回。"""
    # 塞入 120 条共享 tech_stack 维度的旧记忆（> dimension_top_n=50）
    for i in range(120):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    # mock 向量检索返回空（新记忆无语义相似）
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: [])

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,  # 大预算，确保截断来自预筛而非预算
        cfg=cfg, prescreen=_ps_cfg(dimension_top_n=50),
    )
    assert len(groups) == 1
    assert len(groups[0].candidates) == 50  # 恰好 dimension_top_n


def test_prescreen_dimension_candidates_priority_ordered(mem_conn, cfg, monkeypatch):
    """维度候选截断按 priority 降序保留高价值候选。"""
    for i in range(80):
        _insert_existing(mem_conn, f"低价值{i}内容", ["tech_stack"], priority=10)
    _insert_existing(mem_conn, "高价值记忆内容", ["tech_stack"], priority=99)

    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: [])

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,
        cfg=cfg, prescreen=_ps_cfg(dimension_top_n=50),
    )
    cands = groups[0].candidates
    assert len(cands) == 50
    assert cands[0]["priority"] == 99  # 高价值候选优先保留
    assert any(c["content"] == "高价值记忆内容" for c in cands)


# ---------- 2. 向量并集 ----------

def test_prescreen_merges_vector_candidates(mem_conn, cfg, monkeypatch):
    """向量 Top-K 补充维度外的语义候选，并集去重。"""
    dim_id = _insert_existing(mem_conn, "同维度记忆", ["tech_stack"], priority=60)
    # 两条不同维度但语义相似的记忆（向量会召回）
    sem_a = _insert_existing(mem_conn, "语义相似记忆A", ["goals"], priority=60)
    sem_b = _insert_existing(mem_conn, "语义相似记忆B", ["goals"], priority=60)

    fake_vectors = [
        {"memory_id": sem_a, "content": "语义相似记忆A", "priority": 60, "updated_at": "2026-01-01T00:00:00Z", "score": 0.9},
        {"memory_id": sem_b, "content": "语义相似记忆B", "priority": 60, "updated_at": "2026-01-01T00:00:00Z", "score": 0.8},
        {"memory_id": dim_id, "content": "同维度记忆", "priority": 60, "updated_at": "2026-01-01T00:00:00Z", "score": 0.7},
    ]
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: fake_vectors)

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,
        cfg=cfg, prescreen=_ps_cfg(),
    )
    cands = groups[0].candidates
    contents = {c["content"] for c in cands}
    # 并集：维度候选 + 向量候选，去重后含全部三条
    assert "同维度记忆" in contents
    assert "语义相似记忆A" in contents
    assert "语义相似记忆B" in contents
    assert len(cands) == 3  # 无重复


def test_prescreen_candidate_cap(mem_conn, cfg, monkeypatch):
    """候选总数有上限：vector_top_k + dimension_top_n，防单记忆候选失控。"""
    ids = []
    for i in range(100):
        ids.append(_insert_existing(mem_conn, f"旧{i}内容", ["tech_stack"], priority=60))

    fake_vectors = [
        {"memory_id": ids[i], "content": f"向量候选{i}内容", "priority": 60,
         "updated_at": "2026-01-01T00:00:00Z", "score": 0.9 - i * 0.01}
        for i in range(100)
    ]
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: fake_vectors)

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,
        cfg=cfg, prescreen=_ps_cfg(vector_top_k=50, dimension_top_n=50),
    )
    assert len(groups[0].candidates) <= 100  # 50 + 50 上限


# ---------- 3. 降级回退 ----------

def test_prescreen_disabled_full_recall(mem_conn, cfg, monkeypatch):
    """prescreen.enabled=false → 行为与现状一致（全量召回不截断）。"""
    for i in range(120):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    # 即使 mock 了向量，关闭预筛也不应调用
    called = {"embed": False, "vs": False}
    def fake_embed(*a, **kw):
        called["embed"] = True
        return [0.1, 0.2, 0.3]
    def fake_vs(*a, **kw):
        called["vs"] = True
        return []
    monkeypatch.setattr(vector_mod, "embed", fake_embed)
    monkeypatch.setattr(vector_mod, "vector_search", fake_vs)

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,
        cfg=cfg, prescreen=None,  # 未配置 → 完全现状
    )
    assert len(groups[0].candidates) == 120  # 全量召回
    assert not called["embed"] and not called["vs"]


def test_prescreen_embed_failure_falls_back_full_recall(mem_conn, cfg, monkeypatch):
    """embed 不可达（返回 None）→ 回退全量召回，不截断不丢候选（宁贵勿漏）。"""
    for i in range(120):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: None)  # 端点不可达
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: pytest.fail("不应调用"))

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,
        cfg=cfg, prescreen=_ps_cfg(),
    )
    assert len(groups[0].candidates) == 120  # 回退全量


def test_prescreen_vector_search_exception_falls_back_full_recall(mem_conn, cfg, monkeypatch):
    """向量检索抛异常 → 回退全量召回，不崩溃。"""
    for i in range(120):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    def boom(*a, **kw):
        raise RuntimeError("vector_search 挂了")
    monkeypatch.setattr(vector_mod, "vector_search", boom)

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,
        cfg=cfg, prescreen=_ps_cfg(),
    )
    assert len(groups[0].candidates) == 120  # 回退全量


# ---------- 4. resolve_conflicts 端到端（预筛接线） ----------

def test_resolve_conflicts_prescreen_limits_prompt(mem_conn, cfg, monkeypatch):
    """端到端：预筛开启时候选进 LLM 的 prompt 显著变小（候选数受限）。"""
    from sgme.engine.l15 import resolve_conflicts

    for i in range(150):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: [])

    captured: list[str] = []
    def fake_call(fallback_cfg, prompt, chain_name, client=None):
        captured.append(prompt)
        # 对每条新记忆回 store
        n = prompt.count("[新记忆#")
        body = json.dumps([
            {"new_memory_index": i, "candidate_ids": [], "action": "store"}
            for i in range(n)
        ])
        return body, "deepseek", {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}

    import sgme.llm.chain as llm_chain
    monkeypatch.setattr(llm_chain, "call_with_fallback", fake_call)

    cfg["l15"] = {"prescreen": _ps_cfg(dimension_top_n=50)}
    result = resolve_conflicts([_new_mem()], mem_conn, cfg, client=None)
    assert result.error is None
    assert captured, "应发起 LLM 调用"
    # 候选数 = 50（dimension_top_n）；+1 为模板指令中的格式示例"候选#xxx]"
    assert captured[0].count("[候选#") == 51


def test_resolve_conflicts_prescreen_disabled_full_recall_prompt(mem_conn, cfg, monkeypatch):
    """端到端：预筛关闭 → 候选全量进 prompt（现状行为）。"""
    from sgme.engine.l15 import resolve_conflicts

    for i in range(80):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    captured: list[str] = []
    def fake_call(fallback_cfg, prompt, chain_name, client=None):
        captured.append(prompt)
        n = prompt.count("[新记忆#")
        body = json.dumps([
            {"new_memory_index": i, "candidate_ids": [], "action": "store"}
            for i in range(n)
        ])
        return body, "deepseek", {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}

    import sgme.llm.chain as llm_chain
    monkeypatch.setattr(llm_chain, "call_with_fallback", fake_call)

    cfg["l15"] = {"prescreen": {"enabled": False}}
    result = resolve_conflicts([_new_mem()], mem_conn, cfg, client=None)
    assert result.error is None
    assert captured[0].count("[候选#") == 81  # 全量 80 + 模板示例 1


# ---------- 5. fallback=skip_conflict 成本熔断（2026-08-16 T-4x） ----------

def test_prescreen_embed_failure_skip_conflict_clears_candidates(mem_conn, cfg, monkeypatch):
    """embed 不可达 + fallback=skip_conflict → 候选清空（不回退全量召回）。"""
    for i in range(120):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: None)  # 端点不可达
    monkeypatch.setattr(vector_mod, "vector_search", lambda *a, **kw: pytest.fail("不应调用"))

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,
        cfg=cfg, prescreen=_ps_cfg() | {"fallback": "skip_conflict"},
    )
    assert len(groups) == 1
    assert groups[0].candidates == []  # 候选清空 → resolve_conflicts 短路 store


def test_prescreen_vector_search_exception_skip_conflict_clears_candidates(mem_conn, cfg, monkeypatch):
    """向量检索抛异常 + fallback=skip_conflict → 候选清空，不崩溃不烧钱。"""
    for i in range(120):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    def boom(*a, **kw):
        raise RuntimeError("vector_search 挂了")
    monkeypatch.setattr(vector_mod, "vector_search", boom)

    groups, _ = l15.build_candidate_groups(
        mem_conn, [_new_mem()],
        per_memory_budget_tokens=10**7,
        cfg=cfg, prescreen=_ps_cfg() | {"fallback": "skip_conflict"},
    )
    assert len(groups[0].candidates) == 0


def test_resolve_conflicts_skip_conflict_short_circuit_no_llm(mem_conn, cfg, monkeypatch):
    """端到端：embed 不可达 + fallback=skip_conflict → 全部直接 store，零 LLM 调用。"""
    from sgme.engine.l15 import resolve_conflicts

    for i in range(120):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    called = {"llm": False}
    def fake_call(*a, **kw):
        called["llm"] = True
        raise AssertionError("skip_conflict 不应发起 LLM 调用")
    import sgme.llm.chain as llm_chain
    monkeypatch.setattr(llm_chain, "call_with_fallback", fake_call)
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: None)

    cfg["l15"] = {"prescreen": _ps_cfg() | {"fallback": "skip_conflict"}}
    result = resolve_conflicts([_new_mem()], mem_conn, cfg, client=None)
    assert result.error is None
    assert len(result.stored) == 1  # 短路 store，未调 LLM
    assert not called["llm"]


def test_config_merge_prescreen_fallback_default_full_recall():
    """配置合并：fallback 默认 full_recall（向后兼容），显式可覆盖为 skip_conflict。"""
    from sgme import config as sgme_config
    base = sgme_config._merge_l15_config(None)
    assert base["prescreen"]["fallback"] == "full_recall"
    merged = sgme_config._merge_l15_config({"prescreen": {"fallback": "skip_conflict"}})
    assert merged["prescreen"]["fallback"] == "skip_conflict"


# ---------- 6. T-132 预筛降级可观测标记（prescreen_skipped） ----------

def test_prescreen_skip_conflict_records_observable_marker(mem_conn, cfg, monkeypatch):
    """embed 不可达 + fallback=skip_conflict → refine_runs 出现独立 prescreen_skipped 标记。

    验收（T-132）：降级原本走「候选清空→全部 store」短路、不调 LLM、不记任何 run，
    与 LLM 判「无变化」(action_counts['skip']) 同名混淆且完全不可见。
    现独立记一条 run，action_counts={'prescreen_skipped': N}，可被 A/B 观测识别。
    """
    for i in range(120):
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)

    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: None)  # 端点不可达

    cfg["l15"] = {"prescreen": _ps_cfg() | {"fallback": "skip_conflict"}}
    result = l15.resolve_conflicts([_new_mem()], mem_conn, cfg, client=None)

    # 1) 结果层标记
    assert result.prescreen_skipped == 1
    assert len(result.stored) == 1  # 短路 store 行为不变（不丢数据）

    # 2) refine_runs 层独立标记（可观测）
    rows = mem_conn.execute(
        "SELECT action_counts FROM refine_runs WHERE stage='l1_conflict'"
    ).fetchall()
    assert rows, "应记录至少一条 refine_run"
    found = False
    for (ac_json,) in rows:
        ac = json.loads(ac_json or "{}")
        if "prescreen_skipped" in ac:
            assert ac["prescreen_skipped"] == 1
            found = True
    assert found, "refine_runs 中未找到 prescreen_skipped 标记"


def test_prescreen_normal_path_no_skip_marker(mem_conn, cfg, monkeypatch):
    """对照组：向量可用（正常预筛）→ 不记 prescreen_skipped 标记。"""
    existing_ids = [
        _insert_existing(mem_conn, f"旧记忆{i}内容", ["tech_stack"], priority=60)
        for i in range(120)
    ]

    # embed 返回正常向量；vector_search 返回完整候选记录（含 content，可被 prompt 渲染）
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **kw: [0.1, 0.2, 0.3])
    monkeypatch.setattr(
        vector_mod, "vector_search",
        lambda *a, **kw: [
            {"memory_id": existing_ids[i], "content": f"旧记忆{i}内容",
             "priority": 60, "updated_at": "2026-01-01T00:00:00Z", "score": 0.9}
            for i in range(5)
        ],
    )

    cfg["l15"] = {"prescreen": _ps_cfg()}
    result = l15.resolve_conflicts([_new_mem()], mem_conn, cfg, client=None)

    assert result.prescreen_skipped == 0
    rows = mem_conn.execute(
        "SELECT action_counts FROM refine_runs WHERE stage='l1_conflict'"
    ).fetchall()
    for (ac_json,) in rows:
        ac = json.loads(ac_json or "{}")
        assert "prescreen_skipped" not in ac, "正常路径不应出现降级标记"

