r"""ST-58/T-292：`_parse_adjudication_with_repair` 接入层语义。

关键收益：解析失败的原行为是**整批重发 LLM**（token 翻倍，日志累计 55 次，
最终仍失败则默认 store 丢冲突检测）。就地修复成功后**不重发**。

安全约束：修复后必须能解析 **且** 裁决数与批内新记忆数相容；否则抛回原重试链路。
"""

from __future__ import annotations

import json

import pytest

from sgme.engine.l3_adjudicate import AdjudicationError, _parse_adjudication_with_repair


# ---------- L1.5 ----------


def _good(n: int = 1) -> str:
    items = ", ".join(
        '{"new_memory_index": %d, "candidate_ids": [], "action": "store", "reason": "新信息"}' % i
        for i in range(n)
    )
    return f"[{items}]"


def test_normal_parse_no_repair_needed():
    decisions = _parse_adjudication_with_repair(_good(2), expected=2)
    assert len(decisions) == 2
    assert all(d.action == "store" for d in decisions)


def test_repair_path_succeeds_without_retry():
    r"""未转义反斜杠（生产失败形态）→ 就地修复成功，调用方无需重发 LLM。"""
    bad = (
        '[{"new_memory_index": 0, "candidate_ids": ["cid-1"], "action": "update", '
        r'"merged_content": "源码在 C:\work\demo", "reason": "同主体"}]'
    )
    decisions = _parse_adjudication_with_repair(bad, expected=1)
    assert len(decisions) == 1
    assert decisions[0].action == "update"
    assert decisions[0].merged_content == r"源码在 C:\work\demo"


def test_repair_trailing_comma():
    bad = '[{"new_memory_index": 0, "candidate_ids": [], "action": "skip", "reason": "无增量"},]'
    decisions = _parse_adjudication_with_repair(bad, expected=1)
    assert decisions[0].action == "skip"


def test_unrepairable_raises_l15error():
    """括号不配对等结构性破损 → 抛 AdjudicationError（上游走原重试链路）。"""
    with pytest.raises(AdjudicationError):
        _parse_adjudication_with_repair('[{"new_memory_index": 0', expected=1)


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
    with pytest.raises(AdjudicationError):
        _parse_adjudication_with_repair(bad, expected=1)


def test_empty_array_is_valid_but_empty():
    """空数组本身合法（不走修复）→ 返回空列表，由调用方据 len 判断语义。"""
    assert _parse_adjudication_with_repair("[]", expected=1) == []
