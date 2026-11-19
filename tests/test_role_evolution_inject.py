# -*- coding: utf-8 -*-
"""tests/test_role_evolution_inject.py：演化层真实注入（P0 消费端）。

背景（2026-11-09 实测）：演化层 GET/POST 通了，但 assemble 不读——写入
12 条后 role_assemble 产物 delta=0。本组用例锁住消费端：
写进演化层的东西必须出现在 system_prompt 里，且受配置与预算约束。

设计 4.2 分层：基线层（六槽位）→ 演化层（经历/变化）→ 生活块（role_life），
位置即权重，顺序不可乱。
"""
from __future__ import annotations

import json

import pytest

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.data import db
from sgme.operations import care as care_ops

SLOTS = {"情绪反应": "难过时话变少", "语言风格": "短句为主"}


@pytest.fixture()
def roles_dir(tmp_path, monkeypatch):
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    monkeypatch.setattr(sgme_config, "PERSONA_DIR", tmp_path / "personas")
    ed = tmp_path / "data" / "role_evolution"
    monkeypatch.setattr(sgme_config, "ROLE_EVOLUTION_DIR", ed)
    return rd


def _create(role_id="butler", *, slots=None):
    data = {"name": "管家", "description": "照护", "extensions": {"sgme_profile": {}}}
    if slots is not None:
        data["extensions"]["sgme_profile"]["personality_slots"] = slots
    res = care_ops.create_role(role_id, data)
    assert res.ok, res
    return role_id


def _evo_dir():
    return sgme_config.ROLE_EVOLUTION_DIR


def _assemble(role_id, tmp_path, cfg=None):
    mem = db.connect_memory(tmp_path / "data")
    try:
        cfg = cfg or sgme_config.load_config()
        return care_ops.assemble(role_id, mem, cfg)
    finally:
        mem.close()


def test_timeline_and_changes_injected(roles_dir, tmp_path):
    _create(slots=SLOTS)
    roles_mod.append_evolution("butler", "timeline", {"event": "去了趟旧书摊，站着看了很久"}, _evo_dir())
    roles_mod.append_evolution("butler", "timeline", {"event": "晚上煮汤，忘了放盐"}, _evo_dir())
    roles_mod.append_evolution(
        "butler", "values_changes",
        {"change": "更看重陪伴而非效率", "reason": "一次停电夜谈"}, _evo_dir())
    roles_mod.append_evolution(
        "butler", "personality_changes",
        {"slot": "情绪反应", "from": "难过时话变少", "to": "难过时先说出来", "reason": "被追问了一次"},
        _evo_dir())

    out = _assemble("butler", tmp_path)
    assert out.ok, out
    sp = out.data["system_prompt"]
    assert "【近期经历】" in sp
    assert "去了趟旧书摊" in sp
    assert "【近来的变化】" in sp
    assert "更看重陪伴而非效率" in sp
    assert "难过时先说出来" in sp
    assert out.data["evolution_block"].startswith("【近期经历】")
    assert out.data["evolution_items"]["timeline"] == 2
    assert out.data["evolution_items"]["changes"] == 2
    # 顺序：基线层在前，演化层在后（位置即权重）
    assert sp.index("【性格基线】") < sp.index("【近期经历】")


def test_empty_evolution_no_block(roles_dir, tmp_path):
    _create(slots=SLOTS)
    out = _assemble("butler", tmp_path)
    assert out.ok
    assert "【近期经历】" not in out.data["system_prompt"]
    assert out.data["evolution_block"] is None


def test_evolution_inject_disabled(roles_dir, tmp_path):
    _create(slots=SLOTS)
    roles_mod.append_evolution("butler", "timeline", {"event": "去了趟旧书摊"}, _evo_dir())
    cfg = sgme_config.load_config()
    cfg["care"] = {**cfg.get("care", {}), "evolution_inject": {"enabled": False}}
    out = _assemble("butler", tmp_path, cfg)
    assert out.ok
    assert "【近期经历】" not in out.data["system_prompt"]
    assert out.data["evolution_block"] is None


def test_timeline_items_limit_keeps_newest(roles_dir, tmp_path):
    _create(slots=SLOTS)
    for i in range(12):
        roles_mod.append_evolution("butler", "timeline", {"event": f"第{i}件事"}, _evo_dir())
    cfg = sgme_config.load_config()
    cfg["care"] = {**cfg.get("care", {}), "evolution_inject": {"timeline_items": 3}}
    out = _assemble("butler", tmp_path, cfg)
    sp = out.data["system_prompt"]
    assert "第11件事" in sp and "第10件事" in sp and "第9件事" in sp
    assert "第8件事" not in sp and "第0件事" not in sp
    assert out.data["evolution_items"]["timeline"] == 3


def test_reason_truncated_and_switchable(roles_dir, tmp_path):
    _create(slots=SLOTS)
    long_reason = "因为那天" + "很长的说明" * 20
    roles_mod.append_evolution(
        "butler", "values_changes", {"change": "更看重陪伴", "reason": long_reason}, _evo_dir())
    cfg = sgme_config.load_config()
    cfg["care"] = {**cfg.get("care", {}), "evolution_inject": {"reason_max_chars": 12}}
    sp = _assemble("butler", tmp_path, cfg).data["system_prompt"]
    assert "依据：" in sp
    assert "很长的说明很长的说明很长的说明" not in sp

    cfg2 = sgme_config.load_config()
    cfg2["care"] = {**cfg2.get("care", {}), "evolution_inject": {"include_reason": False}}
    sp2 = _assemble("butler", tmp_path, cfg2).data["system_prompt"]
    assert "依据：" not in sp2


def test_corrupt_evolution_degrades(roles_dir, tmp_path):
    _create(slots=SLOTS)
    ed = _evo_dir()
    ed.mkdir(parents=True, exist_ok=True)
    (ed / "butler.json").write_text("{ broken json", encoding="utf-8")
    out = _assemble("butler", tmp_path)
    assert out.ok
    assert out.data["evolution_block"] is None
    assert "【近期经历】" not in out.data["system_prompt"]


def test_no_pronoun_no_time_leak(roles_dir, tmp_path):
    """块内不出现「她/他」人称与绝对时间断言——换了角色也不用改文案。"""
    _create(slots=SLOTS)
    roles_mod.append_evolution("butler", "timeline", {"event": "去了趟旧书摊"}, _evo_dir())
    block = _assemble("butler", tmp_path).data["evolution_block"]
    assert "她" not in block and "他" not in block
    assert "2026" not in block
