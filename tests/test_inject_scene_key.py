"""D-53 替代方案：注入块带 scene_key 分布（场景层退役后的分组信息位）。

覆盖：分布来自实际注入条目（去重后）/ 按条数降序 / 最多 5 项 / 无 scene_key 不加键 /
空 section 不加键 / 不改变取材与 items 内容（纯增量）。
"""
from __future__ import annotations

from sgme.profile.inject import build_inject_blocks


TEMPLATE = {
    "name": "test",
    "sections": [
        {"title": "偏好", "query": {"dimensions": ["preferences"]}},
        {"title": "技术", "query": {"dimensions": ["tech_stack"]}},
    ],
}


def _row(mid, content, scene_key=None, updated_at="2026-11-20T00:00:00Z"):
    r = {"memory_id": mid, "content": content, "updated_at": updated_at}
    if scene_key is not None:
        r["scene_key"] = scene_key
    return r


def test_scenes_distribution_present():
    rows1 = [
        _row("m1", "偏好甲", "preferences:desktop"),
        _row("m2", "偏好乙", "preferences:desktop"),
        _row("m3", "偏好丙", "preferences:mobile"),
    ]
    out = build_inject_blocks(TEMPLATE, [rows1, []])
    block = out["blocks"][0]
    assert block["scenes"] == [
        {"key": "preferences:desktop", "count": 2},
        {"key": "preferences:mobile", "count": 1},
    ], "应按条数降序"


def test_no_scene_key_no_field():
    rows = [_row("m1", "无场景键")]
    out = build_inject_blocks(TEMPLATE, [rows, []])
    assert "scenes" not in out["blocks"][0], "无 scene_key 时不得加键（行为不变）"


def test_counts_only_injected_items():
    """被去重跳过的条目不计入分布（分布描述的是注入内容，不是候选池）。"""
    rows = [
        _row("m1", "同一句话的完整内容", "preferences:desktop"),
        _row("m2", "同一句话的完整内容", "preferences:mobile"),  # 前缀相同 → 被去重跳过
    ]
    out = build_inject_blocks(TEMPLATE, [rows, []])
    block = out["blocks"][0]
    assert len(block["items"]) == 1
    assert block["scenes"] == [{"key": "preferences:desktop", "count": 1}]


def test_top5_cap():
    rows = [_row(f"m{i}", f"内容{i}", f"preferences:src{i}") for i in range(8)]
    out = build_inject_blocks(TEMPLATE, [rows, []])
    assert len(out["blocks"][0]["scenes"]) == 5, "最多 5 项防块膨胀"


def test_items_unchanged():
    """scene 分布只能挂在 block 上，不得改动 items 结构（消费方契约）。"""
    rows = [_row("m1", "偏好甲", "preferences:desktop")]
    out = build_inject_blocks(TEMPLATE, [rows, []])
    item = out["blocks"][0]["items"][0]
    assert "scene_key" not in item and "scenes" not in item
    assert item["content"] == "偏好甲"
    assert item["memory_id"] == "m1"
