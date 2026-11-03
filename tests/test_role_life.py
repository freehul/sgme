"""tests/test_role_life.py：角色离线生活（role_life_events）测试。

覆盖（交接 2026-11-02 文档·任务 B 步 1–2）：
1. DAO：写入 / 列出 / 秘密标记 / 被问才说（shared_at）
2. 三条硬边界：
   - 隔离：角色人生绝不进 memories（用户记忆池）
   - 类型级：内容由调用方保证，DAO 不校验专名（边界在上游）
   - 秘密稳定：is_secret=1 永不进注入；被问才说，说一次即 shared
3. 注入频率闸：每会话 1 条 / 每日上限 / 同一事件不重复给
4. 装配接线：assemble() 把生活块拼进 system_prompt 末尾（适配器零改动也能拿到）
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.data import db as db_mod
from sgme.data import role_life_dao
from sgme.operations import care as care_ops

# ---------- fixtures ----------

@pytest.fixture
def mem_conn(tmp_path):
    mem, session, wiki = db_mod.init_databases(tmp_path / "data")
    yield mem
    for c in (mem, session, wiki):
        db_mod.close(c)


@pytest.fixture
def roles_dir(tmp_path, monkeypatch):
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    return rd


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def butler(roles_dir):
    """最小合法角色卡（CC V2 子集）。"""
    card = {
        "spec": "compose_chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "管家",
            "description": "跟了很多年的管家",
            "system_prompt": "你是{{char}}，{{user}}的管家。",
        },
    }
    card["spec"] = roles_mod.CC_SPEC
    roles_mod.save_role(roles_dir, "butler", card)
    return "butler"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 1. DAO 基础 ----------

def test_add_and_list(mem_conn):
    eid = role_life_dao.add_event(
        mem_conn, "butler", "看到有人在争论本地模型的显存调优", kind="see", mood="有点想插嘴",
    )
    assert eid
    items = role_life_dao.list_events(mem_conn, "butler")
    assert len(items) == 1
    assert items[0]["content"].startswith("看到有人在争论")
    assert items[0]["kind"] == "see"
    assert items[0]["mood"] == "有点想插嘴"
    assert items[0]["is_secret"] == 0
    assert items[0]["injected_at"] is None
    assert items[0]["shared_at"] is None


def test_list_filters(mem_conn):
    role_life_dao.add_event(mem_conn, "butler", "普通见闻")
    role_life_dao.add_event(mem_conn, "butler", "没说出口的事", is_secret=True, why_secret="怕你担心")
    role_life_dao.add_event(mem_conn, "friend", "别人的见闻")
    assert len(role_life_dao.list_events(mem_conn, "butler")) == 2
    assert len(role_life_dao.list_events(mem_conn, "butler", only_secret=True)) == 1
    assert len(role_life_dao.list_events(mem_conn, "butler", only_secret=False)) == 1
    assert len(role_life_dao.list_events(mem_conn, "friend")) == 1


# ---------- 2. 硬边界：隔离 / 秘密 ----------

def test_never_touches_memories(mem_conn):
    """角色人生是虚构设定，绝不能进用户记忆池。"""
    before = mem_conn.execute("select count(*) from memories").fetchone()[0]
    role_life_dao.add_event(mem_conn, "butler", "读到一篇讲小城邮局变迁的文章")
    after = mem_conn.execute("select count(*) from memories").fetchone()[0]
    assert before == after
    # 表独立：不与 memories 共享任何 memory_id 语义
    cols = {r[1] for r in mem_conn.execute("pragma table_info(role_life_events)")}
    assert "memory_id" not in cols


def test_secret_never_picked_for_injection(mem_conn):
    role_life_dao.add_event(mem_conn, "butler", "有件事没说出口", is_secret=True)
    assert role_life_dao.pick_for_injection(mem_conn, "butler") is None


def test_secret_shared_only_once(mem_conn):
    eid = role_life_dao.add_event(
        mem_conn, "butler", "那天在老教授家门口站了很久", is_secret=True, why_secret="还没想明白",
    )
    first = role_life_dao.pick_secret(mem_conn, "butler")
    assert first is not None and first["event_id"] == eid
    assert role_life_dao.mark_shared(mem_conn, eid) is True
    # 说过的秘密不再给
    assert role_life_dao.pick_secret(mem_conn, "butler") is None
    assert role_life_dao.mark_shared(mem_conn, eid) is False  # 幂等：已说过


# ---------- 3. 频率闸 ----------

def test_injection_not_repeated(mem_conn):
    """daily_stable=False（每轮取新条）：同一条不会被给第二次。"""
    eid = role_life_dao.add_event(mem_conn, "butler", "去了趟旧书摊")
    first = role_life_dao.pick_for_injection(mem_conn, "butler", daily_stable=False)
    assert first and first["event_id"] == eid
    assert role_life_dao.pick_for_injection(mem_conn, "butler", daily_stable=False) is None


def test_daily_stable_reuses_same_event_within_day(mem_conn):
    """daily_stable=True（默认，C-1）：当天复用同一条，保 system_prompt 前缀缓存。"""
    now = datetime.now(UTC)
    for i in range(3):
        role_life_dao.add_event(mem_conn, "butler", f"见闻{i}", at=_iso(now - timedelta(hours=i)))
    first = role_life_dao.pick_for_injection(mem_conn, "butler", now=now)
    assert first is not None
    second = role_life_dao.pick_for_injection(mem_conn, "butler", now=now)
    assert second is not None
    assert first["event_id"] == second["event_id"]   # 同一天同一条
    # 次日才换（额度与选条都重算）
    tomorrow = now + timedelta(days=1)
    nxt = role_life_dao.pick_for_injection(mem_conn, "butler", now=tomorrow)
    assert nxt is not None and nxt["event_id"] != first["event_id"]


def test_daily_max_gate(mem_conn):
    now = datetime.now(UTC)
    for i in range(3):
        role_life_dao.add_event(mem_conn, "butler", f"见闻{i}", at=_iso(now - timedelta(hours=i)))
    assert role_life_dao.pick_for_injection(
        mem_conn, "butler", daily_max=1, now=now, daily_stable=False) is not None
    assert role_life_dao.pick_for_injection(
        mem_conn, "butler", daily_max=1, now=now, daily_stable=False) is None
    # 第二天额度恢复
    tomorrow = now + timedelta(days=1)
    assert role_life_dao.pick_for_injection(
        mem_conn, "butler", daily_max=1, now=tomorrow, daily_stable=False) is not None


def test_lookback_window(mem_conn):
    now = datetime.now(UTC)
    role_life_dao.add_event(mem_conn, "butler", "很久以前看到的", at=_iso(now - timedelta(days=30)))
    assert role_life_dao.pick_for_injection(mem_conn, "butler", lookback_days=7, now=now) is None
    # 放宽窗口后可见
    assert role_life_dao.pick_for_injection(mem_conn, "butler", lookback_days=60, now=now) is not None


def test_pick_newest_first(mem_conn):
    now = datetime.now(UTC)
    role_life_dao.add_event(mem_conn, "butler", "旧的", at=_iso(now - timedelta(hours=5)))
    role_life_dao.add_event(mem_conn, "butler", "新的", at=_iso(now - timedelta(hours=1)))
    picked = role_life_dao.pick_for_injection(mem_conn, "butler", now=now)
    assert picked["content"] == "新的"


# ---------- 4. 装配接线 ----------

def test_assemble_injects_life_block(mem_conn, cfg, butler):
    role_life_dao.add_event(mem_conn, "butler", "看到有人在争论本地模型的显存调优", mood="想插嘴")
    res = care_ops.assemble(butler, mem_conn, cfg)
    assert res.ok, res.message
    data = res.data or {}
    assert data["life_block"]["content"].startswith("看到有人在争论")
    assert "看到有人在争论本地模型的显存调优" in data["system_prompt"]
    # 生活块在末尾（位置即权重）
    assert data["system_prompt"].index("你的生活") > data["system_prompt"].index("管家")


def test_assemble_without_life_keeps_prompt_clean(mem_conn, cfg, butler):
    res = care_ops.assemble(butler, mem_conn, cfg)
    assert res.ok
    data = res.data or {}
    assert data["life_block"] is None
    assert "你的生活" not in data["system_prompt"]


def test_assemble_respects_disable_switch(mem_conn, cfg, butler):
    role_life_dao.add_event(mem_conn, "butler", "去了趟旧书摊")
    cfg = dict(cfg)
    cfg["role_life"] = {**(cfg.get("role_life") or {}), "enabled": False}
    res = care_ops.assemble(butler, mem_conn, cfg)
    assert res.ok
    assert (res.data or {})["life_block"] is None


def test_assemble_consumes_only_one_per_assembly(mem_conn, cfg, butler):
    """C-1：默认 daily_stable → 同一天两次装配拿到同一条（缓存友好）。"""
    role_life_dao.add_event(mem_conn, "butler", "见闻甲")
    role_life_dao.add_event(mem_conn, "butler", "见闻乙")
    first = care_ops.assemble(butler, mem_conn, cfg)
    second = care_ops.assemble(butler, mem_conn, cfg)
    a = (first.data or {})["life_block"]["content"]
    b = (second.data or {})["life_block"]["content"]
    assert a == b


def test_assemble_rotates_when_daily_stable_off(mem_conn, cfg, butler):
    """关掉 daily_stable → 回到每轮换一条的旧行为。"""
    cfg = dict(cfg)
    cfg["role_life"] = {**(cfg.get("role_life") or {}), "daily_stable": False}
    role_life_dao.add_event(mem_conn, "butler", "见闻甲")
    role_life_dao.add_event(mem_conn, "butler", "见闻乙")
    a = (care_ops.assemble(butler, mem_conn, cfg).data or {})["life_block"]["content"]
    b = (care_ops.assemble(butler, mem_conn, cfg).data or {})["life_block"]["content"]
    assert a != b


def test_assemble_never_injects_secret(mem_conn, cfg, butler):
    role_life_dao.add_event(mem_conn, "butler", "没说出口的事", is_secret=True, why_secret="怕你担心")
    res = care_ops.assemble(butler, mem_conn, cfg)
    data = res.data or {}
    assert data["life_block"] is None
    assert "没说出口的事" not in data["system_prompt"]


def test_assemble_renders_macro_inside_life_block(mem_conn, cfg, butler):
    """生活块在渲染之后拼接——render=True 时块里的 {{user}} 也必须被替换。"""
    role_life_dao.add_event(mem_conn, "butler", "去了趟旧书摊")
    res = care_ops.assemble(butler, mem_conn, cfg, render=True, user_name="老张")
    assert res.ok, res.message
    prompt = (res.data or {})["system_prompt"]
    assert "你的生活" in prompt
    assert "{{user}}" not in prompt
    assert "老张" in prompt


# ---------- 5. 配置默认值 ----------

def test_config_defaults(cfg):
    rl = cfg.get("role_life")
    assert isinstance(rl, dict)
    assert rl["enabled"] is True
    assert rl["daily_max"] >= 1
    assert rl["lookback_days"] >= 1
