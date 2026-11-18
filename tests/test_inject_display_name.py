"""T-424 · D-6 维度展示消费面（inject 侧 display_name 纯增量透出）测试。

D-6：维度等展示用中文，英文 id 附在边上。inject 此前完全没带 display_name
（grep 0 命中），本组测试锁死：

- 传注册表 → section 块纯增量带 ``dimensions: [{id, display_name}]``（中文为主）；
- 不传注册表（缺省 None）→ 行为完全不变（无 dimensions 键，不炸）；
- 注册表里查不到的维度 → display_name 回退英文 id；
- 既有键集不变（title/items/present 原样）。

UI 并显（MemoryList.vue 中文为主 + 小字 id）由 typecheck+build 门禁覆盖，
不属本测试文件。
"""
from __future__ import annotations

import pytest

from sgme.profile import inject as inject_mod


# ---------- fixtures ----------

REGISTRY = [
    {"id": "identity", "display_name": "身份"},
    {"id": "preferences", "display_name": "偏好"},
    {"id": "goals", "display_name": "目标"},
]

TEMPLATE = {
    "name": "daily",
    "sections": [
        {"title": "核心身份", "query": {"dimensions": ["identity"], "limit": 5}},
        {"title": "偏好与目标", "query": {"dimensions": ["preferences", "mystery_dim"], "limit": 5}},
    ],
}


def _mk_section_result(memory_id: str) -> list[dict]:
    return [{"memory_id": memory_id, "content": f"内容 {memory_id}",
             "updated_at": "2026-11-18T00:00:00Z"}]


# ---------- 透出 / 回退 / 缺省 ----------

def test_display_name_passed_through_blocks():
    """传注册表 → 块带 dimensions[{id, display_name}]，中文为主。"""
    out = inject_mod.build_inject_blocks(
        TEMPLATE, [[], _mk_section_result("m1")], dimensions=REGISTRY)
    b0, b1 = out["blocks"][0], out["blocks"][1]
    assert b0["dimensions"] == [{"id": "identity", "display_name": "身份"}]
    assert {"id": "preferences", "display_name": "偏好"} in b1["dimensions"]
    # 未知维度回退 id（D-6：英文 id 附边上；注册表缺失时 id 即兜底展示）
    assert {"id": "mystery_dim", "display_name": "mystery_dim"} in b1["dimensions"]


def test_no_registry_no_change():
    """不传注册表（None 缺省）→ 行为完全不变：无 dimensions 键、不炸。"""
    out = inject_mod.build_inject_blocks(TEMPLATE, [[], _mk_section_result("m1")])
    for b in out["blocks"]:
        assert "dimensions" not in b
        assert b["title"] and "items" in b and "present" in b  # 既有键集不变
    item = out["blocks"][1]["items"][0]
    assert set(item.keys()) == {"content", "relative_time", "memory_id"}  # item 键集不变


def test_registry_missing_display_name_falls_back_to_id():
    """注册表行缺 display_name 字段 → 回退 id（不炸）。"""
    broken = [{"id": "identity"}]  # 有 id 无 display_name
    out = inject_mod.build_inject_blocks(
        TEMPLATE, [[], []], dimensions=broken)
    assert out["blocks"][0]["dimensions"] == [{"id": "identity", "display_name": "identity"}]


def test_unknown_registry_entries_ignored():
    """注册表含无关维度不影响块内容（只按 section 的 query.dimensions 取）。"""
    extra = REGISTRY + [{"id": "unused", "display_name": "无关"}]
    out = inject_mod.build_inject_blocks(
        TEMPLATE, [[], []], dimensions=extra)
    b0 = out["blocks"][0]
    assert all(d["id"] != "unused" for d in b0["dimensions"])
