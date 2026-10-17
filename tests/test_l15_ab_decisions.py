"""ST-58/T-296：L1.5 决策级 A/B 对照器的语料不变量与对照逻辑单测。

**不跑真 LLM**（真链路对照见 `eval/l15_ab_decisions.py` 实跑报告）——本文件只锁：
① 语料的确定性/覆盖度不变量（否则门禁测不出东西）；
② 一致率/准确率/成本聚合的纯逻辑（这部分错了会给出**假绿灯**，必须锁死）；
③ 单变量保证（臂只改声明的配置项）。
"""

from __future__ import annotations

import json

import pytest

from eval import l15_ab_decisions as H
from eval import l15_decision_corpus as C


# ---------- 语料不变量 ----------


def test_batches_cover_all_four_actions():
    """语料必须覆盖 L1.5 的完整动作空间，否则门禁有盲区。"""
    expects = {b["expect"] for b in C.BATCHES}
    assert {"store", "skip", "update", "merge"} <= expects, f"覆盖不足: {expects}"


def test_batches_have_id_note_expect():
    for b in C.BATCHES:
        assert b["id"] and b["note"] and b["expect"], b
        assert b["new_memories"], b


def test_batch_ids_unique():
    ids = [b["id"] for b in C.BATCHES]
    assert len(ids) == len(set(ids))


def test_pool_ids_unique_and_deterministic():
    ids = [m["memory_id"] for m in C.POOL]
    assert len(ids) == len(set(ids))
    # 固定 memory_id / updated_at 是跨臂可比的前提
    for m in C.POOL:
        assert "updated_day" in m and "dimension_ids" in m


def test_long_tail_and_escape_scenarios_present():
    """两个专项风险场景必须在语料里（B1' 截断尾部 / JSON 转义修复）。"""
    assert any("update_long_tail" == b["id"] for b in C.BATCHES)
    assert any("escape_path" == b["id"] for b in C.BATCHES)
    # 含反斜杠路径的那条池记忆要真的带反斜杠（否则场景形同虚设）
    assert any("\\" in m["content"] for m in C.POOL)


def test_fillers_deterministic_and_same_dimension():
    a = C.make_fillers(5)
    b = C.make_fillers(5)
    assert [x["content"] for x in a] == [x["content"] for x in b]
    assert all(x["dimension_ids"] == ["environment"] for x in a)
    # 填充的 updated_at 必须**比池条目旧** —— 否则会把池条目挤出候选池，
    # 目标候选根本送不到模型眼前，expect 在物理上不可达成（首版踩坑，见模块注释）
    assert max(x["updated_day"] for x in a) < min(m["updated_day"] for m in C.POOL)


def test_pool_entries_survive_candidate_cap():
    """回归锁：池条目必须**恒在候选池内**（按 updated_at DESC 取 top-n 也挤不掉）。

    首版语料让填充比池更新，`pool-0001`/`pool-0008` 被挤出 63 条候选的 top-30/50，
    导致 `skip_subsumed`/`escape_path` 两条 expect 不可达成、准确率被严重低估。
    """
    pool = C.build_pool(60)
    env = [m for m in pool if "environment" in m["dimension_ids"]]
    env_sorted = sorted(env, key=lambda m: -m["updated_day"])
    hand = [m["memory_id"] for m in env if not m["memory_id"].startswith("filler")]
    assert len(env) > 50, "候选数必须超过最大上限，否则截断不发生 → 假绿灯"
    for cap in (30, 50):
        hit = {m["memory_id"] for m in env_sorted[:cap]}
        assert set(hand) <= hit, f"cap={cap} 挤掉了池条目: {set(hand) - hit}"


def test_all_fixture_timestamps_parse_as_iso():
    """时间戳必须是真的 ISO 8601（首版把天数偏移当月内日号拼出 `2026-03-61`）。"""
    from datetime import datetime

    for m in C.build_pool(60):
        t = C._ts(m["updated_day"])
        datetime.fromisoformat(t.replace("Z", "+00:00"))  # 不抛即通过


def test_build_pool_composition():
    assert len(C.build_pool(7)) == len(C.POOL) + 7
    assert len(C.build_pool(7, 5)) == len(C.POOL) + 7 + 5


def test_each_action_family_has_min_samples():
    """T-298：每类动作 ≥10 条，否则一致率/准确率没有统计意义。"""
    from collections import Counter

    dist = Counter(b["expect"] for b in C.BATCHES)
    for action in ("store", "skip", "update", "merge"):
        assert dist[action] >= 10, f"{action} 仅 {dist.get(action)} 条"


def test_targets_unique_and_exist():
    """每个非 store 批必须有**独占**的目标池条目。

    独占是硬约束：L1.5 会落库改池（update/merge 归档目标候选），若两条批共用同一
    池条目，批序就会互相破坏（先跑的归档掉，后跑的找不到目标）。
    """
    ids = {m["memory_id"] for m in C.POOL}
    targets = [b["target"] for b in C.BATCHES if b.get("target")]
    assert len(targets) == len(set(targets)), "目标池条目被多条批共用"
    for t in targets:
        assert t in ids, t
    # store 族不应有目标（主体全新）
    for b in C.BATCHES:
        if b["expect"] == "store":
            assert b.get("target") is None, b["id"]


def test_pool_contents_unique():
    """池内容必须唯一——重复内容会让「两条批共享同一主体」，expect 互相干扰。"""
    conts = [m["content"] for m in C.POOL]
    assert len(conts) == len(set(conts))


def test_boundary_scenario_declared():
    """B2 的风险面必须有语料覆盖：目标候选落在 30/50 之间的边界批。"""
    b = [x for x in C.BATCHES if x["id"] == C.BOUNDARY_BATCH_ID]
    assert len(b) == 1, "缺少边界批"
    assert b[0]["target"] == C.BOUNDARY_TARGET_ID
    assert b[0]["expect"] == "update"
    bf = C.make_boundary_fillers(5)
    # 边界填充必须比目标新（才能把它挤下去），且主体不同（否则模型对它判 update，风险演示不出来）
    target = [m for m in C.POOL if m["memory_id"] == C.BOUNDARY_TARGET_ID][0]
    assert min(x["updated_day"] for x in bf) > target["updated_day"]
    assert all("非用户自有设备" in x["content"] for x in bf)


# ---------- 对照逻辑（错了会出假绿灯） ----------


def _arm(name: str, actions: dict[tuple[str, int], str], **kw) -> dict:
    batches: dict[str, list] = {}
    for (bid, idx), act in actions.items():
        batches.setdefault(bid, []).append(
            {"new_memory_index": idx, "action": act, "reason": "", "idempotent_skip": False}
        )
    return {
        "arm": name,
        "batches": [
            {"batch_id": bid, "expect": kw.get("expect", {}).get(bid), "decisions": ds}
            for bid, ds in batches.items()
        ],
        "runs": kw.get("runs", []),
        "prescreen": kw.get("prescreen", {}),
        "note": "",
    }


def test_compare_detects_identical():
    a = {"a": "store", "b": "update"}
    base = _arm("base", {("x", 0): "store", ("y", 0): "update"})
    arm = _arm("cand", {("x", 0): "store", ("y", 0): "update"})
    out = H.compare(base, arm)
    assert out["total"] == 2 and out["diff"] == 0 and out["consistency"] == 1.0


def test_compare_reports_per_item_diffs():
    """必须能指出「哪条被判成了别的动作」——这是它区别于批次级 action_counts 的价值。"""
    base = _arm("base", {("x", 0): "store", ("y", 0): "update"})
    arm = _arm("cand", {("x", 0): "store", ("y", 0): "skip"})
    out = H.compare(base, arm)
    assert out["diff"] == 1 and out["consistency"] == 0.5
    assert out["diffs"] == [{"batch_id": "y", "index": 0,
                             "baseline": "update", "arm": "skip"}]


def test_compare_treats_missing_as_diff():
    """一臂缺样本（如短路未留痕）必须算差异，不得静默忽略。"""
    base = _arm("base", {("x", 0): "store", ("y", 0): "update"})
    arm = _arm("cand", {("x", 0): "store"})
    out = H.compare(base, arm)
    assert out["total"] == 2 and out["diff"] == 1
    assert out["diffs"][0]["arm"] is None


def test_accuracy_counts_against_expect():
    arm = _arm("cand", {("x", 0): "store", ("y", 0): "store"},
               expect={"x": "store", "y": "update"})
    acc = H._accuracy(arm)
    assert acc["hit"] == 1 and acc["total"] == 2 and acc["rate"] == 0.5
    assert acc["mismatches"][0]["batch_id"] == "y"
    assert acc["mismatches"][0]["expect"] == "update"
    assert acc["mismatches"][0]["got"] == "store"


def test_accuracy_empty_is_none_rate():
    assert H._accuracy(_arm("cand", {}))["rate"] is None


def test_summarize_costs_ignores_null_rows():
    """候选数/耗时列可能为 NULL（短路记录、A2 之前的老行）→ 不得进分母。"""
    arm = _arm("cand", {}, runs=[
        {"candidate_count": 10, "prompt_tokens": 100, "status": "ok"},
        {"candidate_count": None, "prompt_tokens": None, "status": "ok"},
        {"candidate_count": 20, "prompt_tokens": 200, "status": "error"},
    ])
    c = H.summarize_costs(arm)
    assert c["l1_conflict_runs"] == 3
    assert c["candidate_count_avg"] == 15.0
    assert c["candidate_count_max"] == 20
    assert c["prompt_tokens_sum"] == 300
    assert c["errors"] == 1


def test_render_markdown_has_gate_sections():
    base = _arm("baseline", {("x", 0): "store"}, expect={"x": "store"},
                prescreen={"vector_top_k": 50, "dimension_top_n": 50})
    arm = _arm("b2_30_30", {("x", 0): "skip"}, expect={"x": "store"},
               prescreen={"vector_top_k": 30, "dimension_top_n": 30})
    report = {
        "generated_at": "2026-10-17T00:00:00Z", "pool_base": 8, "fillers": 60,
        "pool_size": 68, "use_vector": True,
        "refine": {"backend": "cloud", "base_url": "x", "model": "y"},
        "arms": [
            {"arm": "baseline", "note": "", "prescreen": base["prescreen"],
             "batches": base["batches"], "cost": H.summarize_costs(base),
             "accuracy": H._accuracy(base), "vs_baseline": None},
            {"arm": "b2_30_30", "note": "", "prescreen": arm["prescreen"],
             "batches": arm["batches"], "cost": H.summarize_costs(arm),
             "accuracy": H._accuracy(arm),
             "vs_baseline": H.compare(base, arm)},
        ],
    }
    md = H.render_markdown(report)
    for section in ("决策准确率", "决策一致率", "逐条差异", "逐条裁决明细",
                    "prescreen 上限是否真的卡住"):
        assert section in md, section
    # 未达标要给 ❌（假绿灯防线：一致率 < 0.90 必须显式标出）
    assert "❌" in md


# ---------- 单变量保证 ----------


def test_build_cfg_applies_only_declared_overrides():
    """臂只改声明的字段（单变量）——否则 A/B 归因失效。"""
    kwargs = dict(embed_base_url="http://127.0.0.1:1/v1", embed_model="m",
                  refine_base_url="http://127.0.0.1:1/v1", refine_model="mm",
                  use_vector=False)
    base = H.build_cfg(H.ARMS["baseline"], **kwargs)
    arm = H.build_cfg(H.ARMS["b2_30_30"], **kwargs)
    assert base["l15"]["prescreen"]["vector_top_k"] == 50
    assert arm["l15"]["prescreen"]["vector_top_k"] == 30
    assert arm["l15"]["prescreen"]["dimension_top_n"] == 30
    # 其余段落必须逐字段一致
    assert base["llm"] == arm["llm"]
    assert base["search"] == arm["search"]


def test_build_cfg_cloud_backend_keeps_production_chain():
    """cloud 后端：**保留基线生产链**（含 agnes），不注入本地节点。"""
    cfg = H.build_cfg(H.ARMS["baseline"], embed_base_url="http://127.0.0.1:1/v1",
                      embed_model="m", refine_base_url="http://127.0.0.1:1/v1",
                      refine_model="mm", use_vector=False, refine_backend="cloud")
    providers = [n.get("provider") for n in cfg["llm"]["chains"]["refinement"]]
    assert "lmstudio_local" not in providers, providers
    assert "agnes" in providers, providers
    assert providers[-1] == "rule", providers


def test_build_cfg_local_backend_replaces_chain():
    """local 后端：链被替换为本地节点 + rule 兜底（零云依赖）。"""
    cfg = H.build_cfg(H.ARMS["baseline"], embed_base_url="http://127.0.0.1:1/v1",
                      embed_model="m", refine_base_url="http://127.0.0.1:1/v1",
                      refine_model="mm", use_vector=False, refine_backend="local")
    providers = [n.get("provider") for n in cfg["llm"]["chains"]["refinement"]]
    assert providers == ["lmstudio_local", "rule"]


def test_summarize_costs_reports_providers():
    """必须披露实际 provider——否则「生产模型对照」可能其实是 rule 兜底却看不出来。"""
    arm = _arm("cand", {}, runs=[
        {"provider": "agnes", "candidate_count": 1, "prompt_tokens": 10, "status": "ok"},
        {"provider": "agnes", "candidate_count": 2, "prompt_tokens": 20, "status": "ok"},
        {"provider": "rule", "candidate_count": None, "prompt_tokens": None, "status": "ok"},
    ])
    assert H.summarize_costs(arm)["providers"] == {"agnes": 2, "rule": 1}


def test_probe_ranks_output_shape(monkeypatch, tmp_path):
    """排名探针的输出结构（不真连向量/LLM：桩掉 build_candidate_groups）。"""
    from sgme.engine import l15 as l15_mod

    class _G:
        def __init__(self, ids):
            self.candidates = [{"memory_id": i} for i in ids]

    def _fake_build(mem_conn, new_memories, top_k=50, **kw):
        # 模拟：cap>=40 时目标在内（#3），cap<40 时被剔除
        ids = ["filler-a", "filler-b"] + (["pool-x"] if top_k >= 40 else [])
        return [_G(ids)], False

    monkeypatch.setattr(l15_mod, "build_candidate_groups", _fake_build)
    monkeypatch.setattr(H, "seed_pool", lambda conn, pool: None)
    monkeypatch.setattr(H, "db_mod",
                        type("m", (), {"init_databases": staticmethod(
                            lambda d: (object(), object(), object()))}))
    monkeypatch.setattr(H, "memory_dao", type("m", (), {"import_registry": staticmethod(
        lambda *a, **k: None)}))

    out = H.probe_ranks(
        work_dir=tmp_path, pool=[], batches=[{"id": "b1", "expect": "update", "target": "pool-x",
                                             "new_memories": [{"content": "x"}]}],
        base_cfg={"dimensions": [], "aliases": []}, caps=(30, 50))
    assert out["caps"] == [30, 50]
    row = out["batches"][0]
    assert row["per_cap"]["30"]["included"] is False
    assert row["per_cap"]["50"]["included"] is True
    assert row["per_cap"]["50"]["rank"] == 3
    assert len(out["boundary_candidates"]) == 1


def test_main_report_assembly_end_to_end(monkeypatch, tmp_path):
    """回归：`main()` 拼报告必须能跑通（曾因 provider_confound 误用未计算的键而 KeyError，
    两个臂各跑 40 分钟才在最后一步崩掉——这种「跑完才炸」的 bug 必须有低成本用例守住）。"""
    def _fake_run_arm(name, arm, **kw):  # noqa: ANN001
        return {
            "arm": name, "note": arm.get("note", ""), "prescreen": arm.get("prescreen", {}),
            "batches": [{"batch_id": "b1", "expect": "store", "note": "",
                         "error": None, "provider": "fake",
                         "decisions": [{"new_memory_index": 0, "action": "store",
                                        "candidate_ids": [], "reason": "",
                                        "idempotent_skip": False}],
                         "counts": {"stored": 1, "skipped": 0, "updated": 0,
                                    "merged": 0, "archived": 0}}],
            "runs": [{"provider": "fake", "candidate_count": 3, "prompt_tokens": 10,
                      "status": "ok"}],
        }

    monkeypatch.setattr(H, "run_arm", _fake_run_arm)
    rc = H.main(["--arms", "baseline,b1_minimal", "--out", str(tmp_path / "rep")])
    assert rc == 0
    report = json.loads((tmp_path / "rep" / "report.json").read_text(encoding="utf-8"))
    assert "provider_confound" in report
    assert report["provider_confound"]["baseline"] == {"fake": 1}
    assert report["provider_confound"]["arms"]["b1_minimal"] == {"fake": 1}
    assert (tmp_path / "rep" / "report.md").exists()


def test_arms_registry_shape():
    assert "baseline" in H.ARMS
    for name, arm in H.ARMS.items():
        assert isinstance(arm.get("prescreen"), dict), name
        assert arm.get("note"), name


@pytest.mark.parametrize("bad", ["nope", "baseline,unknown"])
def test_main_rejects_unknown_arm(bad, capsys):
    rc = H.main(["--arms", bad, "--out", "tmp/_never_used"])
    assert rc == 1
