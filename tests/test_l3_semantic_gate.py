# -*- coding: utf-8 -*-
"""D-29 语义闸（l3.rules.semantic_gate）：配置合并 + 纯函数判定 + 接线。

安全边界：默认关闭；shadow 只记日志不改判；无向量 score 时不生效（不猜）。
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.engine import l3_adjudicate


# ---------- 配置合并 ----------

def test_config_default_off():
    cfg = config._merge_l3_config(None)
    sg = cfg["rules"]["semantic_gate"]
    assert sg["enabled"] is False
    assert sg["shadow"] is False
    assert sg["low"] == 0.55 and sg["high"] == 0.95


def test_config_merge_valid():
    cfg = config._merge_l3_config({"rules": {"semantic_gate": {
        "enabled": True, "shadow": True, "low": 0.4, "high": 0.9,
    }}})
    sg = cfg["rules"]["semantic_gate"]
    assert sg["enabled"] is True and sg["shadow"] is True
    assert sg["low"] == 0.4 and sg["high"] == 0.9


def test_config_illegal_combo_falls_back():
    """low >= high 属非法组合 → 整对回退出厂值，不放行错配置。"""
    cfg = config._merge_l3_config({"rules": {"semantic_gate": {"low": 0.9, "high": 0.5}}})
    assert cfg["rules"]["semantic_gate"]["low"] == 0.55
    assert cfg["rules"]["semantic_gate"]["high"] == 0.95


def test_config_ignores_unknown_and_bad_types():
    cfg = config._merge_l3_config({"rules": {"semantic_gate": {
        "enabled": "yes", "shadow": 1, "low": "x", "high": None,
    }}})
    sg = cfg["rules"]["semantic_gate"]
    assert sg["enabled"] is False and sg["shadow"] is False
    assert sg["low"] == 0.55 and sg["high"] == 0.95


# ---------- 纯函数 ----------

def _cand(mid: str, score: float | None, content: str = "用户喜欢深色模式"):
    c = {"memory_id": mid, "content": content}
    if score is not None:
        c["score"] = score
    return c


def test_disabled_gate_returns_none():
    assert l3_adjudicate._semantic_gate_override(
        {"content": "新内容"}, [_cand("m1", 0.99)], {"enabled": False},
    ) is None


def test_no_score_returns_none():
    """维度召回候选没有 score → 不生效（不猜）。"""
    assert l3_adjudicate._semantic_gate_override(
        {"content": "新内容"}, [_cand("m1", None)], {"enabled": True},
    ) is None


def test_low_score_store():
    ov = l3_adjudicate._semantic_gate_override(
        {"content": "完全无关的新内容"},
        [_cand("m1", 0.30), _cand("m2", 0.42)],
        {"enabled": True, "low": 0.55, "high": 0.95},
    )
    assert ov is not None and ov[0] == "store"


def test_high_score_skip():
    ov = l3_adjudicate._semantic_gate_override(
        {"content": "用户偏好深色模式"},
        [_cand("m1", 0.97)],
        {"enabled": True, "low": 0.55, "high": 0.95},
    )
    assert ov is not None and ov[0] == "skip"
    assert ov[1] == "m1"


def test_high_score_with_change_signal_stays_llm():
    """含改口信号时即便高相似也不能直接判重（D-28 优先）。"""
    ov = l3_adjudicate._semantic_gate_override(
        {"content": "用户不再喜欢深色模式，改回浅色"},
        [_cand("m1", 0.97)],
        {"enabled": True, "low": 0.55, "high": 0.95},
    )
    assert ov is None


def test_mid_score_stays_llm():
    assert l3_adjudicate._semantic_gate_override(
        {"content": "用户最近偏好深色主题"},
        [_cand("m1", 0.80)],
        {"enabled": True, "low": 0.55, "high": 0.95},
    ) is None


# ---------- 接线：_apply_l3_rules 里 shadow 不改判 ----------

def test_apply_l3_rules_shadow_keeps_llm(monkeypatch):
    """shadow=true 时：只记日志，group 仍留在 remaining（交 LLM）。"""
    from sgme.engine import l3_rules

    monkeypatch.setattr(l3_adjudicate, "_memory_tags", lambda *a, **k: [])
    monkeypatch.setattr(
        l3_rules, "adjudicate",
        lambda *a, **k: l3_rules.RuleDecision("llm", "x", "测试固定灰区", 0.4),
    )
    g = l3_adjudicate.CandidateGroup(
        new_memory_index=0,
        new_memory={"content": "用户偏好深色模式", "dimensions": ["preferences"]},
        candidates=[_cand("m1", 0.97)],
    )
    cfg = {"l3": {"rules": {"enabled": True, "semantic_gate": {
        "enabled": True, "shadow": True, "low": 0.55, "high": 0.95,
    }}}}
    decisions, remaining = l3_adjudicate._apply_l3_rules(None, [g], set(), cfg)
    assert decisions == {}
    assert remaining == [g]


def test_apply_l3_rules_enforced_replaces_llm(monkeypatch):
    from sgme.engine import l3_rules

    monkeypatch.setattr(l3_adjudicate, "_memory_tags", lambda *a, **k: [])
    monkeypatch.setattr(
        l3_rules, "adjudicate",
        lambda *a, **k: l3_rules.RuleDecision("llm", "x", "测试固定灰区", 0.4),
    )
    g = l3_adjudicate.CandidateGroup(
        new_memory_index=0,
        new_memory={"content": "用户偏好深色模式", "dimensions": ["preferences"]},
        candidates=[_cand("m1", 0.97)],
    )
    cfg = {"l3": {"rules": {"enabled": True, "semantic_gate": {
        "enabled": True, "shadow": False, "low": 0.55, "high": 0.95,
    }}}}
    decisions, remaining = l3_adjudicate._apply_l3_rules(None, [g], set(), cfg)
    assert remaining == []
    assert decisions[0].action == "skip"
    assert decisions[0].candidate_ids == ["m1"]
