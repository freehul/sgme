"""tests/test_routing_dispatch.py：三平面分流分发（v0.3 S2，2026-11-14）。

验收：
1. 灰度开关 refine.routing_enabled=false 时**什么都不做**（默认关）
2. 开关开：route=skill_candidate 的记忆落库后自动进技能候选池（免人工捞）
3. 幂等：重复分发不重复建候选
4. route=memory 的记忆不受影响
5. 不写 skill 类目互链（T6，2026-11-15：占位名悬空引用；互链改由 promote 成功后补写）
6. skills.db 不可用 → 跳过计数，不抛异常、不影响已落库记忆
7. persist_memories 的接线是旁路：分流抛异常也不影响返回

设计依据：docs/plans/设计-v0.3-三平面分流定稿与实施契约.md §5
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine import routing as routing_mod


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def conns(tmp_path):
    mem = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(mem, config.load_config()["dimensions"], config.load_config()["aliases"])
    skills = db_mod.connect_skills(tmp_path)
    yield mem, skills
    mem.close()
    skills.close()


def _enabled(cfg):
    import copy

    c = copy.deepcopy(cfg)
    c.setdefault("refine", {})["routing_enabled"] = True
    return c


def _add_memory(mem, memory_id, *, route="skill_candidate", content=None):
    """插一条最小可用的记忆（route 是分流依据，content 需有动作线索才可能被评高置信）。"""
    now = "2026-11-14T00:00:00Z"
    mem.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity,"
        " created_at, updated_at, status, agent_tag, route)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (memory_id, content or "调 LM Studio 时必须补 /v1 前缀，否则返回 404",
         "fact", 50, "slow", now, now, "active", "test", route),
    )
    mem.commit()


def _candidates(skills):
    return skills.execute("SELECT memory_id, name, status FROM skill_candidates").fetchall()


def test_disabled_by_default_noop(conns, cfg):
    mem, skills = conns
    _add_memory(mem, "m-off")
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "m-off", "route": "skill_candidate"}], mem, cfg,
    )
    assert out["enabled"] is False
    assert _candidates(skills) == []


def test_default_config_routing_off():
    assert config.DEFAULT_REFINE_CONFIG["routing_enabled"] is False
    assert "routing_enabled" in config.SECTION_KEYS["refine"]


def test_enabled_adopts_skill_candidate(conns, cfg):
    mem, skills = conns
    _add_memory(mem, "m-on")
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "m-on", "route": "skill_candidate"}], mem, _enabled(cfg),
        file_id="f-1",
    )
    assert out["enabled"] is True
    assert out["candidates"] == 1 and out["adopted"] == 1
    rows = _candidates(skills)
    assert len(rows) == 1 and rows[0][0] == "m-on"
    # T6：分流不写 skill 类目互链（占位名悬空引用；互链见 test_routing_links.py）
    pages = memory_dao.get_memory_pages(mem, "m-on")
    assert not any(p["kind"] == "skill" for p in pages)


def test_dispatch_is_idempotent(conns, cfg):
    mem, skills = conns
    _add_memory(mem, "m-dup")
    payload = [{"memory_id": "m-dup", "route": "skill_candidate"}]
    first = routing_mod.dispatch_after_persist(payload, mem, _enabled(cfg))
    second = routing_mod.dispatch_after_persist(payload, mem, _enabled(cfg))
    assert first["adopted"] == 1
    assert second["adopted"] == 0 and second["duplicates"] == 1
    assert len(_candidates(skills)) == 1


def test_memory_route_untouched(conns, cfg):
    mem, skills = conns
    _add_memory(mem, "m-person", route="memory", content="用户喜欢深色主题")
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "m-person", "route": "memory"}], mem, _enabled(cfg),
    )
    assert out["candidates"] == 0
    assert _candidates(skills) == []


def test_missing_memory_id_is_skipped(conns, cfg):
    mem, skills = conns
    out = routing_mod.dispatch_after_persist(
        [{"route": "skill_candidate"}], mem, _enabled(cfg),
    )
    assert out["candidates"] == 0
    assert _candidates(skills) == []


def test_missing_memory_row_counts_error_not_raise(conns, cfg):
    """记忆不存在 → adopt 返回失败，计 errors，但绝不抛出。"""
    mem, skills = conns
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "nope", "route": "skill_candidate"}], mem, _enabled(cfg),
    )
    assert out["errors"] == 1 and out["adopted"] == 0


def test_persist_hook_is_side_path(cfg, monkeypatch):
    """persist_memories 的分流接线必须是旁路：抛异常也不影响返回。"""
    from sgme.engine import pipeline as pipeline_mod

    def boom(*a, **k):
        raise RuntimeError("分流炸了")

    monkeypatch.setattr(routing_mod, "dispatch_after_persist", boom)

    class _R:
        memories = [{"memory_id": "x", "route": "skill_candidate"}]
        file_id = "f-x"

    # 不抛异常即通过（内部 try/except 吞掉并记日志）
    pipeline_mod._dispatch_after_persist_safe(_R(), None, cfg)
