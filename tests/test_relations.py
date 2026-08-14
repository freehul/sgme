# -*- coding: utf-8 -*-
"""tests/test_relations.py：T-14 关系类型注册表（registry/relations.yaml）测试。

覆盖：
1. 注册表加载齐全（任务清单 26 种：语义 4 + 结构 5 + 时序 5 + 论证 4 + 项目 5 + 记忆域 3）
2. 每类型字段完整（id/category/description/semantics）
3. load_config 集成（cfg["relations"]）
4. 写入校验：wiki_dao.insert_link 未知 rel_type 拒绝、26 种全接受
5. 向后兼容：原 4 种（similar/extends/references/contradicts）行为不变

注：数据模型文档标题写“原 4 → 24”，但其自身类型清单表为 26 种
（4+5+5+4+5+3）；本测试以清单表为准（与任务枚举一致）。
"""
from __future__ import annotations

import pytest

from sgme import config as config_mod
from sgme.data import db as db_mod
from sgme.data import wiki_dao

# 类型清单（依据 SGME-数据模型设计-v0.1.md §wiki_links T-14 目标表）
REL_TYPES = {
    "语义": {"similar", "extends", "references", "contradicts"},
    "结构": {"parent_of", "child_of", "part_of", "instance_of", "related"},
    "时序": {"before", "after", "causes", "caused_by", "evolves_to"},
    "论证": {"supports", "opposes", "questions", "answers"},
    "项目": {"implements", "fixes", "tracked_by", "blocks", "blocked_by"},
    "记忆域": {"merges_with", "supersedes", "same_as"},
}
ALL_REL_TYPES = {t for ts in REL_TYPES.values() for t in ts}
# 原 4 种（v0.7 既有，向后兼容锚点）
LEGACY_REL_TYPES = {"similar", "extends", "references", "contradicts"}


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect_wiki(tmp_path / "data")
    yield c
    db_mod.close(c)


# ---------- 注册表加载 ----------

def test_relations_registry_complete():
    """注册表加载齐全：26 种类型与清单表完全一致（无缺漏、无多余）。"""
    rels = config_mod.load_relations()
    ids = {r["id"] for r in rels}
    assert ids == ALL_REL_TYPES
    assert len(ids) == len(ALL_REL_TYPES) == 26


def test_relations_category_counts():
    """六类数量与设计一致：4/5/5/4/5/3。"""
    rels = config_mod.load_relations()
    by_cat: dict[str, set] = {}
    for r in rels:
        by_cat.setdefault(r["category"], set()).add(r["id"])
    assert {cat: len(ts) for cat, ts in by_cat.items()} == {
        cat: len(ts) for cat, ts in REL_TYPES.items()
    }
    # 每类型归属类别与清单一致
    for cat, ts in REL_TYPES.items():
        assert by_cat[cat] == ts


def test_relations_entry_fields():
    """每类型含 id/类别/描述/语义说明四要素。"""
    rels = config_mod.load_relations()
    for r in rels:
        assert r["id"] in ALL_REL_TYPES
        assert r["category"] in REL_TYPES
        assert isinstance(r.get("description"), str) and r["description"].strip()
        assert isinstance(r.get("semantics"), str) and r["semantics"].strip()


def test_relations_ids_unique():
    """id 唯一（注册表内无重复）。"""
    rels = config_mod.load_relations()
    ids = [r["id"] for r in rels]
    assert len(ids) == len(set(ids))


def test_load_relations_malformed(tmp_path):
    """格式错误/空注册表 → ValueError（与维度注册表同模式）。"""
    bad = tmp_path / "bad_relations.yaml"
    bad.write_text("foo: bar\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config_mod.load_relations(bad)
    empty = tmp_path / "empty_relations.yaml"
    empty.write_text("relations: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config_mod.load_relations(empty)


def test_load_config_includes_relations():
    """load_config 集成：cfg["relations"] 与独立加载一致。"""
    cfg = config_mod.load_config()
    assert "relations" in cfg
    assert [r["id"] for r in cfg["relations"]] == [
        r["id"] for r in config_mod.load_relations()
    ]
    assert {r["id"] for r in cfg["relations"]} == ALL_REL_TYPES


# ---------- 写入校验（wiki_dao.insert_link） ----------

def test_insert_link_accepts_all_registered_types(conn):
    """注册表内 26 种 rel_type 全部可写入。"""
    for i, rel in enumerate(sorted(ALL_REL_TYPES)):
        wiki_dao.insert_link(conn, f"s{i}", f"t{i}", rel_type=rel, confidence=0.9)
    rows = conn.execute("SELECT rel_type FROM wiki_links").fetchall()
    assert {r["rel_type"] for r in rows} == ALL_REL_TYPES


def test_insert_link_default_legacy(conn):
    """默认 rel_type='similar'（v0.7 默认值）行为不变。"""
    wiki_dao.insert_link(conn, "p1", "p2")
    rows = conn.execute("SELECT rel_type FROM wiki_links").fetchall()
    assert len(rows) == 1 and rows[0]["rel_type"] == "similar"


def test_insert_link_legacy_four_still_work(conn):
    """原 4 种向后兼容：similar/extends/references/contradicts 均可写入。"""
    for rel in sorted(LEGACY_REL_TYPES):
        wiki_dao.insert_link(conn, "p1", "p2", rel_type=rel)
    rows = conn.execute("SELECT rel_type FROM wiki_links").fetchall()
    assert {r["rel_type"] for r in rows} == LEGACY_REL_TYPES


def test_insert_link_rejects_unknown_type(conn):
    """未知 rel_type → ValueError 拒绝，且不落库。"""
    with pytest.raises(ValueError, match="rel_type"):
        wiki_dao.insert_link(conn, "p1", "p2", rel_type="magic_link")
    assert conn.execute("SELECT COUNT(*) FROM wiki_links").fetchone()[0] == 0


def test_insert_link_known_types_repeat_ok(conn):
    """合法类型重复写入不抛错（wiki_links 无 UNIQUE 约束，OR IGNORE 不去重——
    既有 schema 行为，校验不改变写入语义）。"""
    wiki_dao.insert_link(conn, "p1", "p2", rel_type="supersedes")
    wiki_dao.insert_link(conn, "p1", "p2", rel_type="supersedes")
    assert conn.execute("SELECT COUNT(*) FROM wiki_links").fetchone()[0] == 2
