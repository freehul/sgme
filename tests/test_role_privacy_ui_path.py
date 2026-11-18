"""T-425 · D-65 角色隐私授权（工作台 UI 走既有写端点的后端契约锁）测试。

卡片做法：RolesView 加「隐私授权」面板 → 读 privacy_agents → 增删后走
**既有**角色写端点（POST /v1/admin/roles/{id} → operations.create_role →
care.roles.save_role，**不新增后端能力**）。本组测试锁死 UI 将依赖的
后端契约：

1. 非法 agent_id（空串/空白/非字符串条目）被校验拒绝、不落盘；
2. 白名单重复保存幂等（重复加入不产生坏状态，check_access 稳定）；
3. 空列表语义 = 未启用隔离（no_policy 放行，与字段缺失同义）。

红线：不实现「按 agent 隔离记忆」（redesign 另卡）；不碰 care.py 他人未提交改动。
"""
from __future__ import annotations

import pytest

from sgme.care import roles as roles_mod
from sgme.operations import privacy_guard


# ---------- fixtures ----------

@pytest.fixture
def roles_dir(tmp_path):
    d = tmp_path / "roles"
    d.mkdir()
    return d


def _base_data(**overrides) -> dict:
    data = {"name": "测试角色", "description": "用于隐私授权契约测试"}
    data.update(overrides)
    return {"data": data}


# ---------- ① 非法 agent_id 被拒 ----------

@pytest.mark.parametrize("bad", [
    pytest.param([""], id="空串"),
    pytest.param(["  "], id="纯空白"),
    pytest.param(["ok-agent", ""], id="混合空白条目"),
    pytest.param(["ok-agent", 3], id="非字符串条目"),
    pytest.param("zcode", id="误传字符串非数组"),
])
def test_invalid_agent_id_rejected(roles_dir, bad):
    """白名单只收非空字符串数组（既有 validate_role_card 契约，UI 前置依赖）。"""
    with pytest.raises(ValueError, match="privacy_agents"):
        roles_mod.save_role(roles_dir, "r-privacy", _base_data(privacy_agents=bad))


# ---------- ② 重复加入幂等 ----------

def test_duplicate_add_idempotent(roles_dir):
    """重复保存同一白名单 → 卡内容稳定；check_access 判定不漂移。"""
    wl = ["zcode", "hermes"]
    roles_mod.save_role(roles_dir, "r-privacy", _base_data(privacy_agents=wl))
    card1 = roles_mod.get_role(roles_dir, "r-privacy")
    assert card1["data"]["privacy_agents"] == wl
    # UI 幂等再加已存在的 agent → 集合不变（UI 侧去重后等价于原样保存）
    roles_mod.save_role(roles_dir, "r-privacy", _base_data(privacy_agents=["zcode", "hermes", "zcode"]))
    card2 = roles_mod.get_role(roles_dir, "r-privacy")
    # 落盘原样保留（不去重是既有语义；guard 集合语义不受影响）
    assert card2["data"]["privacy_agents"] == ["zcode", "hermes", "zcode"]
    assert privacy_guard.check_access(card2, "zcode") == (True, "whitelist")
    assert privacy_guard.check_access(card2, "hermes") == (True, "whitelist")
    assert privacy_guard.check_access(card2, "dsh") == (False, "not_in_whitelist")


def test_add_remove_roundtrip(roles_dir):
    """UI 增删全链路：加 → 放行；删空 → 回落未启用隔离（不再拒绝任何 agent）。"""
    roles_mod.save_role(roles_dir, "r-privacy", _base_data(privacy_agents=["zcode"]))
    card = roles_mod.get_role(roles_dir, "r-privacy")
    assert privacy_guard.check_access(card, "zcode")[0] is True
    assert privacy_guard.check_access(card, "hermes")[0] is False
    # UI 删除唯一授权 → 保存空数组 → no_policy 放行
    roles_mod.save_role(roles_dir, "r-privacy", _base_data(privacy_agents=[]))
    card2 = roles_mod.get_role(roles_dir, "r-privacy")
    ok, reason = privacy_guard.check_access(card2, "hermes")
    assert ok is True and reason == "no_policy"


# ---------- ③ 空列表语义 ----------

def test_empty_list_semantics(roles_dir):
    """空数组与字段缺失同义：未启用隔离（no_policy），resolve 返回空集合。"""
    roles_mod.save_role(roles_dir, "r-empty", _base_data(privacy_agents=[]))
    card_empty = roles_mod.get_role(roles_dir, "r-empty")
    roles_mod.save_role(roles_dir, "r-missing", _base_data())
    card_missing = roles_mod.get_role(roles_dir, "r-missing")
    for card in (card_empty, card_missing):
        assert privacy_guard.resolve_allowed_agents(card) == set()
        ok, reason = privacy_guard.check_access(card, "any-agent")
        assert ok is True and reason == "no_policy"


def test_blank_entries_do_not_grant_access(roles_dir):
    """含空白条目的数组若已落盘（历史脏数据），空白身份不获得授权。"""
    card = {"data": {"name": "x", "description": "y", "privacy_agents": ["zcode", "  "]}}
    assert privacy_guard.resolve_allowed_agents(card) == {"zcode"}
