"""tests/test_routing_links.py：T6 skill 互链时机修复（2026-11-15）。

背景：engine/routing.py 分流成功后用候选占位名（exp-xxx）写 memory_pages 互链，
开关一开就产生悬空引用——技能并不存在，链没有意义（生产 241 条互链全指向真
技能是存量脚本的手笔，分流自己写出来的只会是占位名）。

改法（任务清单-技能自动命名-20261115.md T6，方案 A）：
1. dispatch 阶段不再写 skill 类目互链（wiki 分支不动——页是当场建的，真存在）；
2. review_skill_candidate promote 成功后补写，ref_id = 最终技能名。

验收：
1. 开关开 + route=skill_candidate → memory_pages **不新增** skill 类目（候选池照常入）
2. promote 成功后 → 出现 kind='skill' 且 ref_id=最终技能名
3. 存量 memory_pages 行不受影响（既不删除也不改写）
"""

from __future__ import annotations

import copy

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine import routing as routing_mod
from sgme.operations import skill_candidates as sc_ops
from sgme.skills_hub import SkillsHub, parse_skills_hub_config


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def conns(tmp_path):
    mem = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(
        mem, config.load_config()["dimensions"], config.load_config()["aliases"]
    )
    skills = db_mod.connect_skills(tmp_path)
    yield mem, skills
    mem.close()
    skills.close()


@pytest.fixture
def hub(tmp_path):
    c = parse_skills_hub_config({"skills_hub": {"enabled": True, "path": str(tmp_path / "hub")}})
    return SkillsHub(c)


def _enabled(cfg):
    c = copy.deepcopy(cfg)
    c.setdefault("refine", {})["routing_enabled"] = True
    return c


def _add_memory(mem, memory_id, *, route="skill_candidate"):
    now = "2026-11-15T00:00:00Z"
    mem.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity,"
        " created_at, updated_at, status, agent_tag, route)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (memory_id, "调 LM Studio 时必须补 /v1 前缀，否则返回 404",
         "fact", 50, "slow", now, now, "active", "test", route),
    )
    mem.commit()


def _skill_pages(mem, memory_id):
    return [p for p in memory_dao.get_memory_pages(mem, memory_id) if p["kind"] == "skill"]


def test_dispatch_does_not_write_skill_link(conns, cfg):
    """分流只进候选池，不再写 skill 类目互链（占位名悬空引用的根）。"""
    mem, skills = conns
    _add_memory(mem, "m-t1")
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "m-t1", "route": "skill_candidate"}], mem, _enabled(cfg),
    )
    assert out["adopted"] == 1  # 候选池照常入
    assert _skill_pages(mem, "m-t1") == []


def test_promote_success_writes_skill_link(conns, hub):
    """互链改落 promote 成功后：技能真存在了，链才有意义。"""
    mem, skills = conns
    now = "2026-11-15T00:00:00Z"
    skills.execute(
        "INSERT INTO skill_candidates (memory_id, name, description, content, category,"
        " tags, confidence, status, source, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("m-t2", "test-skill-alpha", "测试候选技能", "---\nname: test-skill-alpha\n---\n\n正文",
         "testing", "[]", 0.9, "accepted", "memory_route", now, now),
    )
    skills.commit()
    r = sc_ops.review_skill_candidate(mem, skills, "m-t2", "promoted", hub=hub)
    assert r.ok, f"晋级应成功: {r.message}"
    pages = _skill_pages(mem, "m-t2")
    assert len(pages) == 1
    assert pages[0]["ref_id"] == "test-skill-alpha"
    assert pages[0]["relation"] == "derived_from"


def test_promote_failure_leaves_no_link(conns, hub, monkeypatch):
    """落成失败（技能不存在）→ 不写互链（链的存在以技能存在为前提）。"""
    mem, skills = conns
    now = "2026-11-15T00:00:00Z"
    skills.execute(
        "INSERT INTO skill_candidates (memory_id, name, description, content, category,"
        " tags, confidence, status, source, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("m-t4", "test-skill-beta", "测试候选技能", "---\nname: test-skill-beta\n---\n\n正文",
         "testing", "[]", 0.9, "accepted", "memory_route", now, now),
    )
    skills.commit()

    def boom(name, content):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(hub, "put_skill", boom)
    r = sc_ops.review_skill_candidate(mem, skills, "m-t4", "promoted", hub=hub)
    assert not r.ok
    assert _skill_pages(mem, "m-t4") == []


def test_existing_links_untouched(conns, cfg):
    """存量互链（生产 241 条指向真技能）不受分流影响：不删不改。"""
    mem, skills = conns
    _add_memory(mem, "m-t3")
    memory_dao.link_memory_pages(
        mem, "m-t3",
        [{"kind": "skill", "ref_id": "legacy-real-skill"}],
        relation="derived_from",
    )
    routing_mod.dispatch_after_persist(
        [{"memory_id": "m-t3", "route": "skill_candidate"}], mem, _enabled(cfg),
    )
    assert [p["ref_id"] for p in _skill_pages(mem, "m-t3")] == ["legacy-real-skill"]


def test_wiki_branch_link_untouched(conns, cfg):
    """wiki 分支互链不动（页是当场建的，真存在）——防止 T6 改过头。"""
    mem, skills = conns
    _add_memory(mem, "m-t5", route="wiki_candidate")
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "m-t5", "route": "wiki_candidate",
          "content": "部署 SGME 时必须先停 Gateway，否则锁库"}], mem, _enabled(cfg),
    )
    assert out["wiki_written"] == 1
    wiki_pages = [p for p in memory_dao.get_memory_pages(mem, "m-t5") if p["kind"] == "wiki"]
    assert len(wiki_pages) == 1 and wiki_pages[0]["ref_id"] == "refine-m-t5"
