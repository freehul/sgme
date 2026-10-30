"""L2 拆条规则测试（SGME 2.0 D-21 / D-22）。

规则表四条判据各有用例，另补两条兜底：未知类型不拆、事实无独立时间锚不拆。
"""

from __future__ import annotations

import pytest

from sgme.engine.l2_split import SplitDecision, decide_split

MAIN = {"content": "用户昨天在诊所做了例行复查", "occurred_at": "2026-10-20", "feeling": "安心"}


@pytest.mark.parametrize("ftype", ["约束", "偏好"])
def test_long_lived_facts_always_split(ftype):
    d = decide_split({"type": ftype, "content": "用户要求删除操作走回收站"}, MAIN)
    assert d.split is True
    assert "必拆" in d.reason
    assert d.route is None


def test_status_stays_in_main():
    d = decide_split({"type": "状态", "content": "用户这几天腰疼"}, MAIN)
    assert d.split is False
    assert "必不拆" in d.reason


def test_experience_routes_to_skill():
    d = decide_split({"type": "经验", "content": "先分段摘要再总装，绝不直接分析长视频"}, MAIN)
    assert d.split is True
    assert d.route == "skill_candidate"


def test_fact_with_own_anchor_splits_as_event():
    d = decide_split({"type": "事实", "content": "用户去年住过院", "occurred_at": "2025-11"}, MAIN)
    assert d.split is True
    assert "独立时间锚" in d.reason


def test_fact_without_anchor_stays():
    d = decide_split({"type": "事实", "content": "用户所在城市有多家医院"}, MAIN)
    assert d.split is False
    assert "不拆兜底" in d.reason


def test_fact_sharing_main_anchor_stays():
    d = decide_split({"type": "事实", "content": "复查结果正常", "occurred_at": "2026-10-20"}, MAIN)
    assert d.split is False


def test_habit_splits_with_pending_note():
    d = decide_split({"type": "习惯", "content": "用户每天早晨走五公里"}, MAIN)
    assert d.split is True
    assert "待确认" in d.reason


def test_idea_and_unknown_do_not_split():
    assert decide_split({"type": "想法", "content": "在考虑换个方案"}, MAIN).split is False
    assert decide_split({"content": "没有类型"}, MAIN).split is False
    assert decide_split({"type": "自创类型", "content": "x"}, MAIN).split is False


def test_decision_is_immutable():
    d = decide_split({"type": "偏好", "content": "x"}, MAIN)
    assert isinstance(d, SplitDecision)
    with pytest.raises(Exception):
        d.split = False  # type: ignore[misc]
