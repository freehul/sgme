# -*- coding: utf-8 -*-
"""tests/test_backfill_facts.py：T-148 facts 批量回填 单元测试（mock LLM，不发真请求）。

覆盖：
- 批量响应正常解析（facts 合法三元组）
- 坏 JSON 首次失败 → 重试后成功（call_openai_compatible 桩切换响应）
- 缺 id 跳过（响应未包含某输入 id → dropped 记录 + 无该条结果）
- 断点续跑：--output 已存在的 id 跳过（幂等）
- 空白归一化 + F1 精确匹配（gate 口径）
- facts 非数组/三元组字段缺失 → 丢弃并按空处理
- 顶层非数组 / 空响应 → 解析错误
- gate 门禁宏平均 F1 汇总

全部离线：以 monkeypatch 替换 llm_provider.call_openai_compatible，不触碰真实端点。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
ONEOFF_DIR = SCRIPTS_DIR / "oneoff"


def _load_script(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# 让 scripts/ 与 scripts/oneoff/ 进 sys.path 供 relative import
sys.path.insert(0, str(SCRIPTS_DIR))

bf = _load_script("backfill_facts_mod", SCRIPTS_DIR / "backfill_facts.py")
gate = _load_script("facts_gate_check_mod", ONEOFF_DIR / "facts_gate_check.py")


# ---------------------------------------------------------------------------
# 解析：正常 / 容错
# ---------------------------------------------------------------------------

def test_parse_batch_normal():
    rows = [{"memory_id": "m1", "content": "张伟在腾讯工作"},
            {"memory_id": "m2", "content": "SGME 部署在群晖 NAS"}]
    resp = json.dumps([
        {"id": "m1", "facts": [{"subject": "张伟", "predicate": "任职于", "object": "腾讯"}]},
        {"id": "m2", "facts": [{"subject": "SGME", "predicate": "部署于", "object": "群晖 NAS"}]},
    ], ensure_ascii=False)
    ok, dropped, errors = bf.parse_batch_response(resp, ["m1", "m2"])
    assert errors == []
    assert len(ok) == 2
    assert ok[0]["memory_id"] == "m1"
    assert ok[0]["facts"] == [{"subject": "张伟", "predicate": "任职于", "object": "腾讯"}]


def test_parse_batch_strips_code_fence():
    resp = '```json\n[{"id": "m1", "facts": [{"subject": "A", "predicate": "P", "object": "O"}]}]\n```'
    ok, dropped, errors = bf.parse_batch_response(resp, ["m1"])
    assert errors == [] and len(ok) == 1


def test_parse_batch_missing_id_skipped():
    """响应只回传 m1，未回传 m2 → m2 记为 dropped，m1 正常产出。"""
    resp = json.dumps([
        {"id": "m1", "facts": [{"subject": "A", "predicate": "P", "object": "O"}]},
    ], ensure_ascii=False)
    ok, dropped, errors = bf.parse_batch_response(resp, ["m1", "m2"])
    assert errors == []
    assert [o["memory_id"] for o in ok] == ["m1"]
    assert any(d["id"] == "m2" for d in dropped)


def test_parse_batch_drops_bad_triples():
    """三元组字段缺失 / 非 dict → dropped；facts 非数组 → 按空处理。"""
    resp = json.dumps([
        {"id": "m1", "facts": [
            {"subject": "A", "predicate": "P", "object": "O"},   # 合法
            {"subject": "", "predicate": "P", "object": "O"},     # 空 subject → drop
            {"subject": "B"},                                     # 缺字段 → drop
            "not-a-dict",                                         # → drop
        ]},
        {"id": "m2", "facts": "oops"},
    ], ensure_ascii=False)
    ok, dropped, errors = bf.parse_batch_response(resp, ["m1", "m2"])
    assert errors == []
    m1 = next(o for o in ok if o["memory_id"] == "m1")
    assert m1["facts"] == [{"subject": "A", "predicate": "P", "object": "O"}]
    m2 = next(o for o in ok if o["memory_id"] == "m2")
    assert m2["facts"] == []
    assert any(d["id"] == "m1" for d in dropped)  # 有坏三元组被丢弃


def test_parse_batch_non_array_is_error():
    resp = '{"not": "array"}'
    ok, dropped, errors = bf.parse_batch_response(resp, ["m1"])
    assert ok == [] and errors and "不是 JSON 数组" in errors[0]


def test_parse_batch_empty_is_error():
    ok, dropped, errors = bf.parse_batch_response("", ["m1"])
    assert ok == [] and errors == ["空响应"]


# ---------------------------------------------------------------------------
# 断点续跑
# ---------------------------------------------------------------------------

def test_checkpoint_skips_existing_ids(tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text(
        json.dumps({"memory_id": "m1", "facts": []}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    done = bf._read_existing_ids(out)
    assert done == {"m1"}

    in_rows = [
        {"memory_id": "m1", "content": "a"},
        {"memory_id": "m2", "content": "张伟在腾讯工作"},
    ]
    todo = [r for r in in_rows if r["memory_id"] not in done]
    assert [r["memory_id"] for r in todo] == ["m2"]


def test_append_result_and_reread(tmp_path):
    out = tmp_path / "sub" / "out.jsonl"
    bf.append_result(out, {"memory_id": "m9", "facts": [{"subject": "S", "predicate": "P", "object": "O"}]})
    bf.append_result(out, {"memory_id": "m10", "facts": []})
    assert bf._read_existing_ids(out) == {"m9", "m10"}
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# LLM 调用：容错重试
# ---------------------------------------------------------------------------

def test_run_batch_llm_bad_json_retry_then_success(monkeypatch):
    """首次返回坏 JSON，重试后返回合法 JSON → 成功结果 + token 记账。"""
    calls = {"n": 0}
    valid = json.dumps([{"id": "m1", "facts": [{"subject": "张伟", "predicate": "任职于", "object": "腾讯"}]}], ensure_ascii=False)

    def fake_call(prompt, node, rules, client=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json at all", {"total_tokens": 100}
        return valid, {"total_tokens": 110}

    monkeypatch.setattr(bf.llm_provider, "call_openai_compatible", fake_call)
    ok, dropped, errors, tokens = bf.run_batch_llm({}, {"model": "x"}, "p", ["m1"])
    assert calls["n"] == 2           # 重试了一次
    assert errors == [] and len(ok) == 1
    assert tokens == 210             # 两次用量累计


def test_run_batch_llm_llm_failure_reported(monkeypatch):
    def fake_call(prompt, node, rules, client=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(bf.llm_provider, "call_openai_compatible", fake_call)
    ok, dropped, errors, tokens = bf.run_batch_llm({}, {"model": "x"}, "p", ["m1"])
    assert ok == [] and errors and "boom" in errors[0]


def test_run_batch_llm_dry_run_no_call(monkeypatch):
    called = {"n": 0}
    orig = bf.llm_provider.call_openai_compatible

    def fake_call(*a, **k):
        called["n"] += 1
        return orig(*a, **k)
    monkeypatch.setattr(bf.llm_provider, "call_openai_compatible", fake_call)
    ok, dropped, errors, tokens = bf.run_batch_llm({}, {"model": "x"}, "p", ["m1"], dry_run=True)
    assert called["n"] == 0          # dry-run 不调 LLM
    assert ok == [] and errors and "dry-run" in errors[0]


# ---------------------------------------------------------------------------
# 提示词渲染
# ---------------------------------------------------------------------------

def test_build_batch_prompt_renders_numbered_memories():
    rows = [{"memory_id": "m1", "content": "A"}, {"memory_id": "m2", "content": "B"}]
    prompt = bf.build_batch_prompt(rows, "前置\n{{memories}}\n末尾")
    assert prompt == "前置\nm1. A\nm2. B\n末尾"
    assert "{{memories}}" not in prompt


# ---------------------------------------------------------------------------
# Gate 门禁 F1
# ---------------------------------------------------------------------------

def _facts(*triples):
    return [{"subject": s, "predicate": p, "object": o} for (s, p, o) in triples]


def test_sample_f1_exact_and_whitespace_normalized():
    single = _facts(("张伟", "任职于", "腾讯"), ("张伟", "负责", "AI平台"))
    batch = _facts(("张伟", "任职于", " 腾讯 "), ("张伟", " 负责 ", "AI 平台"))
    m = gate.sample_f1(single, batch)
    assert m["n_single"] == 2 and m["n_batch"] == 2
    assert m["inter"] == 2           # 空白归一化后精确匹配
    assert m["f1"] == 1.0


def test_sample_f1_partial():
    single = _facts(("A", "P", "O1"), ("A", "P", "O2"))
    batch = _facts(("A", "P", "O1"), ("A", "P", "O3"))
    m = gate.sample_f1(single, batch)
    assert m["inter"] == 1
    assert m["precision"] == m["recall"] == 0.5
    assert m["f1"] == 0.5


def test_aggregate_f1_macro_average():
    per = [
        {"precision": 1.0, "recall": 1.0, "f1": 1.0, "n_batch": 1, "n_single": 1, "inter": 1},
        {"precision": 0.5, "recall": 0.5, "f1": 0.5, "n_batch": 2, "n_single": 2, "inter": 1},
    ]
    agg = gate.aggregate_f1(per)
    assert agg["f1"] == 0.75
    assert agg["samples"] == 2


def test_run_gate_with_stub_and_report(tmp_path):
    """dry 桩：stub 在单条与批量下应产出相同三元组 → 门禁通过（F1=1.0）。"""
    rows = [
        {"memory_id": "m1", "content": "张伟在腾讯工作"},
        {"memory_id": "m2", "content": "李雷住在上海"},
    ]

    def stub(method, batch):
        return gate._stub_results(method, batch)

    per, _single_map, _batch_map = gate.run_gate(rows, None, None, _FakeStore(), dry=True, stub=stub)
    agg = gate.aggregate_f1(per)
    assert agg["f1"] == 1.0

    report = tmp_path / "gate.md"
    text = gate.write_report(report, rows, agg, per, dry=True)
    assert "宏平均 F1" in text and "PASS" not in text  # write_report 不含 PASS 字样
    assert report.exists()


class _FakeStore:
    """桩 PromptStore.get：返回与现网同源的工作副本提示词。"""
    def get(self, stage):
        from sgme.prompts.manager import PromptStore
        return PromptStore().get(stage)
