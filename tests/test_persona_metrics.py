# -*- coding: utf-8 -*-
"""tests/test_persona_metrics.py：D-25 七指标骨架的确定性回归。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from eval.persona import loader
from eval.persona.metrics import evaluate

GOLD = Path(__file__).resolve().parents[1] / "eval" / "persona" / "cases_v001_draft.yaml"


def _gold():
    return loader.load_cases(GOLD)["cases"]


def _perfect(gold):
    return {
        c["id"]: {
            "memories": copy.deepcopy(c["expected"]["memories"]),
            "causal": copy.deepcopy(c["expected"].get("causal") or []),
            "traits": copy.deepcopy(c["expected"].get("traits") or []),
        }
        for c in gold
    }


def _case(gold, cid):
    return next(c for c in gold if c["id"] == cid)


def test_perfect_predictions_all_metrics_green():
    gold = _gold()
    out = evaluate(gold, _perfect(gold))
    m = out["metrics"]
    assert m["false_store_rate"] == 0.0
    assert m["component_f1"] == 1.0
    assert m["miss_rate"] == 0.0
    assert m["dimension_consistency"] == 1.0
    assert m["domain_accuracy"] == 1.0
    assert m["causal_false_link_rate"] == 0.0
    assert m["stereotype_false_positive_rate"] == 0.0
    assert m["subject_accuracy"] == 1.0
    assert m["forget_recall"] is None
    assert [n for n, c in out["checks"].items() if c["passed"] is False] == []


def test_false_store_on_tech_case():
    gold = _gold()
    preds = _perfect(gold)
    preds["pa-004"]["memories"] = [{
        "content": "GIL 是 CPython 全局解释器锁",
        "dimensions": ["skills"], "main_dimension": "skills",
        "memory_type": "episodic", "certainty": "confirmed",
        "subject": "self", "route": "memory",
    }]
    out = evaluate(gold, preds)
    assert out["metrics"]["false_store_rate"] == 1 / 3
    assert out["checks"]["false_store_rate"]["passed"] is False


def test_missing_memory_raises_miss_rate():
    gold = _gold()
    preds = _perfect(gold)
    preds["pa-001"]["memories"] = []
    out = evaluate(gold, preds)
    assert out["metrics"]["miss_rate"] > 0
    assert out["metrics"]["component_recall"] < 1


def test_dimension_and_domain_mismatch():
    gold = _gold()
    preds = _perfect(gold)
    mem = preds["pa-001"]["memories"][0]
    # 黄金 pa-001：dimensions=[status]，main_dimension=status
    mem["dimensions"] = ["status", "goals"]
    out = evaluate(gold, preds)
    assert out["metrics"]["dimension_consistency"] < 1
    assert out["metrics"]["domain_accuracy"] == 1  # 主维未变

    mem2 = copy.deepcopy(_perfect(gold)["pa-001"]["memories"][0])
    mem2["main_dimension"] = "focus"
    preds2 = _perfect(gold)
    preds2["pa-001"]["memories"][0] = mem2
    out2 = evaluate(gold, preds2)
    assert out2["metrics"]["domain_accuracy"] < 1


def test_extra_causal_edge_counts_as_false_link():
    gold = _gold()
    preds = _perfect(gold)
    preds["pa-007"]["causal"] = preds["pa-007"]["causal"] + [
        {"from": "天气", "to": "回滚"}
    ]
    out = evaluate(gold, preds)
    assert out["metrics"]["causal_false_link_rate"] > 0


def test_overclaim_and_subject_flip_hit_proxy_metrics():
    gold = _gold()
    preds = _perfect(gold)
    # pa-006 第二条黄金 certainty=tentative → 预测 confirmed 记过报
    preds["pa-006"]["memories"][1]["certainty"] = "confirmed"
    # pa-020 第一条黄金 subject=others → 预测改成 self
    preds["pa-020"]["memories"][0]["subject"] = "self"
    out = evaluate(gold, preds)
    assert out["metrics"]["stereotype_false_positive_rate"] > 0
    assert out["metrics"]["subject_accuracy"] < 1


def test_preference_recall_drops_when_preference_memory_missing():
    gold = _gold()
    preds = _perfect(gold)
    # pa-008 的记忆维度含 preferences
    preds["pa-008"]["memories"] = []
    out = evaluate(gold, preds)
    assert out["metrics"]["preference_recall"] is not None
    assert out["metrics"]["preference_recall"] < 1
