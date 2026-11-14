"""tests/test_routing_wiki.py：T2 wiki 写入通道（三缺口补齐 2026-11-14）。

验收（依据 docs/plans/任务清单-三缺口补齐-20261114.md T2）：
1. 开关开 + route=wiki_candidate → wiki_pages 出现一条 status='draft'
2. 重跑同一记忆 → 不产生第二页（page_id=refine-{memory_id} 幂等）
3. 草稿不出现在 wiki 检索（search_wiki_fts 只认 active）
4. promote_page 转正后能搜到
5. memory_pages 有 kind='wiki' / relation='derived_from' 一条
6. wiki.db 不可用 → 计 skipped，不抛、不影响已落库记忆
7. 低置信（route_confidence<0.6）→ 走 inbox，不进 wiki
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao, wiki_dao
from sgme.engine import routing as routing_mod
from sgme.wiki import fts as wiki_fts

UNIQUE_TOKEN = "wikidraftzeta9x"


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def conns(tmp_path):
    mem = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(
        mem, config.load_config()["dimensions"], config.load_config()["aliases"]
    )
    wiki = db_mod.connect_wiki(tmp_path)
    yield mem, wiki
    db_mod.close(mem)
    db_mod.close(wiki)


def _enabled(cfg):
    import copy

    c = copy.deepcopy(cfg)
    c.setdefault("refine", {})["routing_enabled"] = True
    return c


def _wiki_memory(memory_id, *, route="wiki_candidate", content=None, agent_tag="test", confidence=None):
    m: dict = {
        "memory_id": memory_id,
        "route": route,
        "agent_tag": agent_tag,
        "content": content or f"SGME 知识条目：草稿态默认不进检索 {UNIQUE_TOKEN}",
    }
    if confidence is not None:
        m["route_confidence"] = confidence
    return m


def _draft_rows(wiki):
    return wiki.execute(
        "SELECT page_id, title, status, page_type, source_type, updated_by, description"
        " FROM wiki_pages WHERE page_id LIKE 'refine-%'"
    ).fetchall()


def test_wiki_candidate_writes_draft_page(conns, cfg):
    mem, wiki = conns
    out = routing_mod.dispatch_after_persist(
        [_wiki_memory("m-w1")], mem, _enabled(cfg), file_id="f-w",
    )
    assert out["enabled"] is True
    assert out["candidates"] == 1
    assert out["wiki_written"] == 1
    rows = _draft_rows(wiki)
    assert len(rows) == 1
    page_id, title, status, page_type, source_type, updated_by, description = rows[0]
    assert page_id == "refine-m-w1"
    assert status == "draft"
    assert page_type == "topic"
    assert source_type == "refine"
    assert updated_by == "agent_test"
    assert UNIQUE_TOKEN in description or UNIQUE_TOKEN in title


def test_wiki_replay_no_second_page(conns, cfg):
    """重跑同一记忆 → insert_page 按 page_id 幂等更新，不产生第二页。"""
    mem, wiki = conns
    payload = [_wiki_memory("m-dup")]
    first = routing_mod.dispatch_after_persist(payload, mem, _enabled(cfg))
    second = routing_mod.dispatch_after_persist(payload, mem, _enabled(cfg))
    assert first["wiki_written"] == 1
    assert second["wiki_written"] == 1
    assert len(_draft_rows(wiki)) == 1


def test_draft_not_searchable_until_promoted(conns, cfg):
    mem, wiki = conns
    routing_mod.dispatch_after_persist([_wiki_memory("m-s1")], mem, _enabled(cfg))
    wiki_fts.init_wiki_fts(wiki)
    hits = wiki_fts.search_wiki_fts(wiki, UNIQUE_TOKEN)
    assert [h["page_id"] for h in hits] == []  # 草稿不进检索
    # 转正后立即可搜
    assert wiki_dao.promote_page(wiki, "refine-m-s1", reviewer="lead") is True
    hits2 = wiki_fts.search_wiki_fts(wiki, UNIQUE_TOKEN)
    assert [h["page_id"] for h in hits2] == ["refine-m-s1"]


def test_memory_pages_linked(conns, cfg):
    mem, wiki = conns
    routing_mod.dispatch_after_persist([_wiki_memory("m-link")], mem, _enabled(cfg))
    pages = memory_dao.get_memory_pages(mem, "m-link")
    assert any(
        p["kind"] == "wiki" and p["ref_id"] == "refine-m-link"
        and p["relation"] == "derived_from"
        for p in pages
    )


def test_wiki_db_unavailable_skips_not_raises(conns, cfg, monkeypatch):
    """wiki.db 不可用 → 计 skipped，不抛、不影响已落库记忆。"""
    mem, _ = conns
    mem.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity,"
        " created_at, updated_at, status, route) VALUES (?,?,?,?,?,?,?,?,?)",
        ("m-broken", "内容", "fact", 50, "slow", "2026-11-14T00:00:00Z",
         "2026-11-14T00:00:00Z", "active", "wiki_candidate"),
    )
    mem.commit()

    def boom(data_dir=None):
        raise RuntimeError("wiki.db 炸了")

    import sgme.data.db as db_pkg
    monkeypatch.setattr(db_pkg, "connect_wiki", boom)
    out = routing_mod.dispatch_after_persist(
        [_wiki_memory("m-broken")], mem, _enabled(cfg),
    )
    assert out["skipped"] == 1 and out["errors"] == 0 and out["wiki_written"] == 0
    # 已落库记忆不受影响
    assert mem.execute(
        "SELECT COUNT(*) FROM memories WHERE memory_id='m-broken'"
    ).fetchone()[0] == 1


def test_low_confidence_goes_inbox_not_wiki(conns, cfg):
    mem, wiki = conns
    out = routing_mod.dispatch_after_persist(
        [_wiki_memory("m-low", confidence=0.4)], mem, _enabled(cfg),
    )
    assert out["inbox"] == 1 and out["wiki_written"] == 0
    assert _draft_rows(wiki) == []
    rows = mem.execute("SELECT route_guess FROM routing_inbox WHERE source_ref='m-low'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "wiki_candidate"
