"""记忆 → 知识页/技能 关联表（memory_pages）落库与读取。

用户定（2026-10-31）：技术细节与踩坑经验归技能库/知识库，记忆只留摘要 + 索引。
索引由提炼阶段按会话内沉淀痕迹自动挂（零 LLM），这里守的是**落库与读取**：
1. insert_memory(sunk_refs=...) 写 memory_pages（幂等）
2. get_memory_pages 读得回来（注入渲染「详情见 …」用）
3. 老库迁移幂等，不因重复调用报错
"""

from __future__ import annotations

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao


@pytest.fixture
def conn(tmp_path):
    cfg = sgme_config.load_config()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    yield mem
    for c in (mem, sess, wiki):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _insert(conn, content: str, sunk_refs=None) -> str:
    return memory_dao.insert_memory(
        conn,
        content=content,
        memory_type="fact",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["tech_stack"],
        occurred_at=None,
        prompt_version="test",
        extra={"type": "经验", "subject": "self", "route": "skill_candidate"},
        sunk_refs=sunk_refs,
    )


def test_insert_writes_links(conn):
    mid = _insert(conn, "GLM 参数口径：用 reasoning_effort=low", [
        {"kind": "wiki", "ref_id": "page-glm-1"},
        {"kind": "skill", "ref_id": "sgme-operations"},
    ])
    got = memory_dao.get_memory_pages(conn, mid)
    assert {(r["kind"], r["ref_id"]) for r in got} == {
        ("wiki", "page-glm-1"), ("skill", "sgme-operations"),
    }
    assert all(r["relation"] == "details" for r in got)


def test_links_are_idempotent(conn):
    refs = [{"kind": "wiki", "ref_id": "page-1"}]
    mid = _insert(conn, "某个技术决策摘要", refs)
    n = memory_dao.link_memory_pages(conn, mid, refs)
    assert n == 0, "重复挂同一页不应重复计数"
    assert len(memory_dao.get_memory_pages(conn, mid)) == 1


def test_no_refs_means_no_rows(conn):
    mid = _insert(conn, "普通事实，没有详情页")
    assert memory_dao.get_memory_pages(conn, mid) == []


def test_migration_is_idempotent(tmp_path):
    data = tmp_path / "data"
    for _ in range(2):
        mem, sess, wiki = db_mod.init_databases(data)
        for c in (mem, sess, wiki):
            c.close()
    mem, sess, wiki = db_mod.init_databases(data)
    names = {r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memory_pages" in names
    for c in (mem, sess, wiki):
        c.close()
