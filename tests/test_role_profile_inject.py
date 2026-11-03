# -*- coding: utf-8 -*-
"""tests/test_role_profile_inject.py：B283 角色基线层（六槽位）真实注入回归。

设计 4.2：extensions.sgme_profile 是「呈现层」——**注入只用六槽位**；
传记/三观/爱好/语气只在工作台编辑，不直接进 system_prompt。
"""
from __future__ import annotations

import pytest

from sgme import config as sgme_config
from sgme.data import db
from sgme.operations import care as care_ops

SLOTS = {
    "语言风格": "短句为主，先给结论",
    "情绪反应": "难过时话变少",
    "价值观取舍": "诚实优先于安慰",
    "社交姿态": "熟人话多，生人先观察",
    "冲突处理": "先听完再表态",
    "亲密表达": "用行动表达，不说软话",
}


@pytest.fixture()
def roles_dir(tmp_path, monkeypatch):
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    monkeypatch.setattr(sgme_config, "PERSONA_DIR", tmp_path / "personas")
    return rd


def _create(role_id="butler", *, slots=None, biography=None):
    data = {
        "name": "管家",
        "description": "照护",
        "extensions": {"sgme_profile": {}},
    }
    prof = data["extensions"]["sgme_profile"]
    if slots is not None:
        prof["personality_slots"] = slots
    if biography is not None:
        prof["biography"] = biography
    res = care_ops.create_role(role_id, data)
    assert res.ok, res
    return role_id


def _assemble(role_id, tmp_path, cfg=None):
    mem = db.connect_memory(tmp_path / "data")
    try:
        cfg = cfg or sgme_config.load_config()
        return care_ops.assemble(role_id, mem, cfg)
    finally:
        mem.close()


def test_six_slots_injected_into_system_prompt(roles_dir, tmp_path):
    _create(slots=SLOTS)
    out = _assemble("butler", tmp_path)
    assert out.ok, out
    assert "【性格基线】" in out.data["system_prompt"]
    assert "语言风格：短句为主" in out.data["system_prompt"]
    assert "亲密表达：用行动表达" in out.data["system_prompt"]
    assert out.data["profile_slots_used"] == 6
    assert out.data["profile_block"].startswith("【性格基线】")


def test_biography_alone_not_injected(roles_dir, tmp_path):
    _create(biography="出生于南方小城，后来搬到海边。")
    out = _assemble("butler", tmp_path)
    assert out.ok
    assert "【性格基线】" not in out.data["system_prompt"]
    assert "出生于南方小城" not in out.data["system_prompt"]
    assert out.data["profile_block"] is None


def test_profile_inject_disabled(roles_dir, tmp_path):
    _create(slots=SLOTS)
    cfg = sgme_config.load_config()
    cfg["care"] = {**cfg.get("care", {}), "profile_inject": {"enabled": False}}
    out = _assemble("butler", tmp_path, cfg)
    assert out.ok
    assert "【性格基线】" not in out.data["system_prompt"]


def test_max_chars_truncates(roles_dir, tmp_path):
    big = {**SLOTS, "语言风格": "短句" * 200}
    _create(slots=big)
    cfg = sgme_config.load_config()
    cfg["care"] = {**cfg.get("care", {}), "profile_inject": {"enabled": True, "max_chars": 40}}
    out = _assemble("butler", tmp_path, cfg)
    block = out.data["profile_block"]
    assert block and block.endswith("…")
    assert len(block) <= 41


def test_render_replaces_macros_in_slots(roles_dir, tmp_path):
    _create(slots={**SLOTS, "亲密表达": "对{{user}}用行动表达"})
    mem = db.connect_memory(tmp_path / "data")
    try:
        cfg = sgme_config.load_config()
        out = care_ops.assemble("butler", mem, cfg, render=True, user_name="主人")
        assert out.ok
        assert "对主人用行动表达" in out.data["system_prompt"]
        assert "{{user}}" not in out.data["profile_block"]
    finally:
        mem.close()
