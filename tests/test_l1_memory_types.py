"""L1 记忆类型白名单测试（2026-09-12：新增 fact 类型，承载会话中的客观事实）。

背景：LongMemEval 诊断显示生产提示词只提取「用户记忆」（persona/episodic/instruction），
助手给出的客观事实（实体名/数字/属性）没有记忆通道 → 低召回。新增 fact 类型后，
白名单必须放行 fact，未知类型仍回落 persona（既有行为不变）。
"""
from __future__ import annotations

from sgme import config as sgme_config
from sgme.engine.l1 import VALID_MEMORY_TYPES, _validate_item


def _cfg():
    return sgme_config.load_config()


def _sample(**over):
    item = {"content": "Veja 使用来自亚马逊雨林的野生橡胶。",
            "dimensions": None, "memory_type": "fact", "priority": 70,
            "time_velocity": "static"}
    item.update(over)
    return item


def test_fact_type_in_whitelist():
    assert "fact" in VALID_MEMORY_TYPES


def test_fact_type_is_accepted():
    cfg = _cfg()
    dim_id = cfg["dimensions"][0]["id"]
    out = _validate_item(_sample(dimensions=[dim_id]), cfg["dimensions"])
    assert out is not None
    assert out["memory_type"] == "fact"


def test_unknown_type_still_falls_back_to_persona():
    cfg = _cfg()
    dim_id = cfg["dimensions"][0]["id"]
    out = _validate_item(_sample(dimensions=[dim_id], memory_type="bogus"), cfg["dimensions"])
    assert out is not None
    assert out["memory_type"] == "persona"
