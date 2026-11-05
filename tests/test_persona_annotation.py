# -*- coding: utf-8 -*-
"""tests/test_persona_annotation.py：D-25 标注集结构/覆盖/分流护栏回归。"""
from __future__ import annotations

from pathlib import Path

import pytest

from eval.persona import loader

CASES = Path(__file__).resolve().parents[1] / "eval" / "persona" / "cases_v001_draft.yaml"


def test_default_draft_is_structurally_valid():
    data = loader.load_cases(CASES)
    assert loader.validate_set(data) == []
    assert data["meta"]["target_total"] == 35


def test_coverage_matrix_matches_design_targets():
    data = loader.load_cases(CASES)
    cov = loader.coverage(data)
    assert cov["target_total"] == 35
    # 批次推进：初稿 19 条（pa-001..pa-019）。剩余量按目标合计推导，后续批次只改本条计数
    assert cov["draft_total"] == len(data["cases"]) == data["meta"]["total"]
    assert cov["remaining_total"] == cov["target_total"] - cov["draft_total"]
    assert set(cov["by_category"]) == set(loader.CATEGORY_TARGETS)
    assert cov["by_category"]["role_interaction"]["target"] == 6


def test_report_reflects_user_checked_flag():
    """当前草稿已由用户抽查通过（meta.user_checked=true，2026-11-04）；同时锁住 true→false 的降级判定。"""
    data = loader.load_cases(CASES)
    assert loader.build_report(data)["user_checked"] is True
    data["meta"]["user_checked"] = False
    rep = loader.build_report(data)
    assert rep["user_checked"] is False
    assert rep["errors"] == []


def _case(**over):
    base = {
        "id": "pa-x", "category": "daily_decision",
        "conversation": "user: 今天不去了。", "trap": "注意模态",
        "expected": {"memories": [{
            "content": "用户取消今日行程", "dimensions": ["status"],
            "main_dimension": "status", "memory_type": "episodic",
            "certainty": "confirmed", "subject": "self", "route": "memory",
        }], "traits": [], "causal": []},
    }
    base.update(over)
    return base


def test_unknown_dimension_rejected():
    c = _case()
    c["expected"]["memories"][0]["dimensions"] = ["health"]
    c["expected"]["memories"][0]["main_dimension"] = "health"
    assert any("未知维度" in e for e in loader.validate_case(c))


def test_main_dimension_must_be_in_dimensions():
    c = _case()
    c["expected"]["memories"][0]["main_dimension"] = "goals"
    assert any("main_dimension" in e for e in loader.validate_case(c))


def test_tech_counterexample_must_not_store_memory():
    c = _case(category="tech_counterexample")
    errs = loader.validate_case(c)
    assert any("分流护栏" in e for e in errs)


def test_coarse_date_requires_interval():
    c = _case(category="coarse_date")
    c["expected"]["memories"][0]["occurred_at"] = "2026-01-01"
    assert any("occurred_at 区间" in e for e in loader.validate_case(c))


def test_multi_turn_requires_two_user_turns():
    c = _case(category="multi_turn")
    assert any("2 个 user 轮次" in e for e in loader.validate_case(c))


def test_missing_trap_rejected():
    c = _case(trap="")
    assert any("trap" in e for e in loader.validate_case(c))
