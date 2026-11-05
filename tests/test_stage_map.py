# -*- coding: utf-8 -*-
"""2.0 分层口径（stage_map）：唯一口径源的回归测试。

背景：2026-11-05 用户定「2.0 没有 L0/L1.5；L1=捕获/剪枝、L2=提取、L3=裁决、L4=梦境」，
代码 stage 名是 1.x 遗留 —— 本测试锁死映射，防再对错层。
"""

from __future__ import annotations

from sgme.engine.stage_map import (
    KNOWN_STAGE_KEYS,
    STAGE_CANONICAL,
    canonical_stage,
    stage_key_of,
    stage_label,
)


def test_core_mapping():
    assert stage_label("l1_extraction") == "L2 提取"
    assert stage_label("l1_conflict") == "L3 裁决"
    assert stage_label("dream") == "L4 梦境"
    assert stage_label("l2_scene") == "遗留场景聚合"
    assert stage_label("tier0_summary") == "用户画像"


def test_canonical_keys():
    assert canonical_stage("l1_extraction") == "L2_extract"
    assert canonical_stage("l1_conflict") == "L3_adjudicate"
    assert canonical_stage("l2_scene") == "legacy_scene"


def test_reverse_lookup():
    assert stage_key_of("L2_extract") == "l1_extraction"
    assert stage_key_of("L3_adjudicate") == "l1_conflict"
    assert stage_key_of("nope") is None


def test_unknown_and_empty_are_explicit():
    assert stage_label("") == ""
    assert stage_label(None) == ""
    # 未知 stage 原样返回：不猜、不吞，便于发现新增阶段未登记
    assert stage_label("future_stage") == "future_stage"
    assert canonical_stage("future_stage") == "future_stage"


def test_no_legacy_naming_in_labels():
    """展示名里不得再出现 L0 / L1.5 / L2 场景等废弃编号。"""
    for key in KNOWN_STAGE_KEYS:
        label = stage_label(key)
        assert "L0" not in label
        assert "L1.5" not in label
        assert "l2_extract.5" not in label.lower()


def test_known_keys_match_db_stages():
    """KNOWN_STAGE_KEYS 覆盖现网 refine_runs 的已知 stage。"""
    assert set(KNOWN_STAGE_KEYS) >= {
        "l1_extraction", "l1_conflict", "l2_scene", "tier0_summary",
    }
    assert STAGE_CANONICAL["l1_extraction"][0] == "L2"
