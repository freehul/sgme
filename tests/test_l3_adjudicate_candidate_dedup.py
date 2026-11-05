# -*- coding: utf-8 -*-
"""候选集内去重（l3_adjudicate.candidate_dedup）：只合并归一化正文相同的候选。

不变量：不减少不同内容候选（不引入漏判风险）；默认关闭（零回归）。
"""

from __future__ import annotations

from sgme import config
from sgme.engine import l3_adjudicate


def _g(cands):
    return l3_adjudicate.CandidateGroup(new_memory_index=0, new_memory={"content": "新"}, candidates=cands)


def test_config_default_off():
    assert config._merge_l3_adjudicate_config(None)["candidate_dedup"]["enabled"] is False
    merged = config._merge_l3_adjudicate_config({"candidate_dedup": {"enabled": True}})
    assert merged["candidate_dedup"]["enabled"] is True
    bad = config._merge_l3_adjudicate_config({"candidate_dedup": {"enabled": "yes"}})
    assert bad["candidate_dedup"]["enabled"] is False


def test_dedup_same_content_keeps_first():
    groups = [_g([
        {"memory_id": "m1", "content": "用户喜欢深色模式"},
        {"memory_id": "m2", "content": "用户喜欢深色模式"},
        {"memory_id": "m3", "content": "用户喜欢浅色模式"},
    ])]
    assert l3_adjudicate._dedupe_group_candidates(groups) == 1
    assert [c["memory_id"] for c in groups[0].candidates] == ["m1", "m3"]


def test_dedup_normalizes_punct_and_space():
    groups = [_g([
        {"memory_id": "m1", "content": "用户喜欢深色模式。"},
        {"memory_id": "m2", "content": "用户喜欢 深色模式"},
    ])]
    assert l3_adjudicate._dedupe_group_candidates(groups) == 1


def test_dedup_does_not_merge_different_content():
    groups = [_g([
        {"memory_id": "m1", "content": "用户喜欢深色模式"},
        {"memory_id": "m2", "content": "用户喜欢深色模式但只在晚上"},
    ])]
    assert l3_adjudicate._dedupe_group_candidates(groups) == 0
    assert len(groups[0].candidates) == 2


def test_dedup_handles_empty_and_multi_groups():
    groups = [
        _g([{"memory_id": "m1", "content": ""}, {"memory_id": "m2", "content": ""}]),
        _g([{"memory_id": "m3", "content": "a"}, {"memory_id": "m4", "content": "a"}]),
    ]
    assert l3_adjudicate._dedupe_group_candidates(groups) == 1
    assert [len(g.candidates) for g in groups] == [2, 1]
