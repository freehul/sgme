r"""ST-58/T-292：`_parse_l15_with_repair` / `_parse_l2_with_repair` 接入层语义。

关键收益：解析失败的原行为是**整批重发 LLM**（token 翻倍，日志累计 55 次，
最终仍失败则默认 store 丢冲突检测）。就地修复成功后**不重发**。

安全约束：修复后必须能解析 **且** 裁决数与批内新记忆数相容；否则抛回原重试链路。
"""

from __future__ import annotations

import json

import pytest

from sgme.engine.l15 import L15Error, _parse_l15_with_repair
from sgme.engine.l2 import L2Error


# ---------- L1.5 ----------


def _good(n: int = 1) -> str:
    items = ", ".join(
        '{"new_memory_index": %d, "candidate_ids": [], "action": "store", "reason": "新信息"}' % i
        for i in range(n)
    )
    return f"[{items}]"


def test_normal_parse_no_repair_needed():
    decisions = _parse_l15_with_repair(_good(2), expected=2)
    assert len(decisions) == 2
    assert all(d.action == "store" for d in decisions)


def test_repair_path_succeeds_without_retry():
    r"""未转义反斜杠（生产失败形态）→ 就地修复成功，调用方无需重发 LLM。"""
    bad = (
        '[{"new_memory_index": 0, "candidate_ids": ["cid-1"], "action": "update", '
        r'"merged_content": "源码在 C:\work\demo", "reason": "同主体"}]'
    )
    decisions = _parse_l15_with_repair(bad, expected=1)
    assert len(decisions) == 1
    assert decisions[0].action == "update"
    assert decisions[0].merged_content == r"源码在 C:\work\demo"


def test_repair_trailing_comma():
    bad = '[{"new_memory_index": 0, "candidate_ids": [], "action": "skip", "reason": "无增量"},]'
    decisions = _parse_l15_with_repair(bad, expected=1)
    assert decisions[0].action == "skip"


def test_unrepairable_raises_l15error():
    """括号不配对等结构性破损 → 抛 L15Error（上游走原重试链路）。"""
    with pytest.raises(L15Error):
        _parse_l15_with_repair('[{"new_memory_index": 0', expected=1)


def test_repair_rejected_when_count_exceeds_expected():
    """修复后裁决数超过批内新记忆数 → 视为不可信，抛回重试。

    注意 `\\P`（非法）而非 `\\b`——后者是合法 JSON 转义，原文可解析就不走修复路径。
    """
    bad = (
        '[{"new_memory_index": 0, "action": "store", "reason": "a"}, '
        r'{"new_memory_index": 1, "action": "store", "reason": "D:\Proj"}]'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)  # 先证明原文确实破损
    with pytest.raises(L15Error):
        _parse_l15_with_repair(bad, expected=1)


def test_empty_array_is_valid_but_empty():
    """空数组本身合法（不走修复）→ 返回空列表，由调用方据 len 判断语义。"""
    assert _parse_l15_with_repair("[]", expected=1) == []


# ---------- L2：已具备同类能力，**不**新增实现（防漂移） ----------


def test_l2_already_handles_invalid_escape():
    r"""L2 早在 2026-08-06 就内置 `_parse_json_lenient`（无效 \X 转义 → 双反斜杠）。

    本批因此**不给 L2 另写一份修复**（AGENTS.md：禁止另写一份防漂移）。
    本用例锁定该既有能力，防止将来被误删。
    """
    from sgme.engine.l2 import _parse_l2_output

    bad = r'[{"action": "update", "target_scene_id": "s1", "merged_content": "涉及 C:\work\demo 的部署"}]'
    actions = _parse_l2_output(bad)
    assert actions[0]["action"] == "update"
    assert r"C:\work\demo" in actions[0]["merged_content"]


def test_l2_still_raises_on_non_json():
    """L2 对彻底非 JSON 的输出依旧抛错（宽松 ≠ 无条件接受）。"""
    from sgme.engine.l2 import _parse_l2_output

    with pytest.raises(L2Error):
        _parse_l2_output("完全不是 JSON")
