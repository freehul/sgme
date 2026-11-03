# -*- coding: utf-8 -*-
"""tests/test_care_levels.py：D-61 关怀三档（角色卡 care_level）回归。

口径（设计 §6.4 / D-61）：
- 档位是**角色卡字段**（extensions.sgme_care.care_level），三值 silent/session/push；
- 新角色默认 silent；非法值拒绝落盘；老卡缺字段 = silent（默认不打扰）；
- role_assemble 扁平暴露 care_level（消费方按档位决定说话时机），
  care_policy 里也保留结构化值。
"""
from __future__ import annotations

import pytest

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.data import db
from sgme.operations import care as care_ops


@pytest.fixture()
def roles_dir(tmp_path, monkeypatch):
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    monkeypatch.setattr(sgme_config, "PERSONA_DIR", tmp_path / "personas")
    return rd


def _card(level: str | None = None, **data_extra):
    data = {"name": "管家", "description": "测试角色", **data_extra}
    if level is not None:
        data["extensions"] = {"sgme_care": {"care_level": level}}
    return {"data": data}


# ---------- 校验与读取 ----------


def test_validate_accepts_three_levels():
    assert roles_mod.CARE_LEVELS == ("silent", "session", "push")
    for level in roles_mod.CARE_LEVELS:
        assert roles_mod.validate_role_card(_card(level)) == []


def test_validate_rejects_unknown_level():
    errors = roles_mod.validate_role_card(_card("aggressive"))
    assert any("care_level" in e for e in errors)


def test_get_care_level_default_and_explicit():
    legacy = _card()  # 老卡：完全没有 sgme_care
    assert roles_mod.get_care_level(legacy) == "silent"
    assert roles_mod.get_care_level(_card("push")) == "push"
    assert roles_mod.get_care_level(None) == "silent"
    # 其它关怀键存在但无 care_level → 仍默认 silent
    other = {"data": {"name": "x", "description": "y",
                      "extensions": {"sgme_care": {"frequency": "weekly"}}}}
    assert roles_mod.get_care_level(other) == "silent"


# ---------- 新建角色默认档 ----------


def test_create_role_defaults_silent(roles_dir):
    res = care_ops.create_role("butler", {"name": "管家", "description": "照护"})
    assert res.ok, res
    card = roles_mod.get_role(roles_dir, "butler")
    assert roles_mod.get_care_level(card) == "silent"
    items = roles_mod.list_roles(roles_dir)
    assert items[0]["care_level"] == "silent"


def test_create_role_persists_explicit_level(roles_dir):
    res = care_ops.create_role(
        "companion",
        {"name": "伴侣", "description": "陪聊",
         "extensions": {"sgme_care": {"care_level": "session"}}},
    )
    assert res.ok, res
    card = roles_mod.get_role(roles_dir, "companion")
    assert roles_mod.get_care_level(card) == "session"
    assert card["data"]["extensions"]["sgme_care"]["care_level"] == "session"


def test_create_role_rejects_invalid_level(roles_dir):
    res = care_ops.create_role(
        "bad",
        {"name": "坏卡", "description": "d",
         "extensions": {"sgme_care": {"care_level": "always_on"}}},
    )
    assert not res.ok
    assert roles_mod.get_role(roles_dir, "bad") is None, "非法档位不得落盘"


# ---------- 装配暴露 ----------


def test_assemble_exposes_care_level(roles_dir, tmp_path):
    care_ops.create_role(
        "butler",
        {"name": "管家", "description": "照护",
         "extensions": {"sgme_care": {"care_level": "push"}}},
    )
    mem = db.connect_memory(tmp_path / "data")
    try:
        cfg = sgme_config.load_config()
        out = care_ops.assemble("butler", mem, cfg)
        assert out.ok, out
        assert out.data["care_level"] == "push"
        assert out.data["care_policy"]["care_level"] == "push"
    finally:
        mem.close()
