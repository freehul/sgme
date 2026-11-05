"""2.0 字段「白名单穿透」回归（T-319）。

背景：2.0 元数据要穿四层才落库，其中三层用白名单重建 dict：
解析后（records_to_items）→ 归一化（refine_file）→ L1.5 存储（l3_adjudicate._store_memory）
→ INSERT（memory_dao.insert_memory）。实测三层都曾静默吃掉字段：一次真实跑批
产出 22 条记忆，type/subject/route 全为 NULL，而日志无任何报错。

本文件断言两处最容易被漏改的地方：
1. `refine_file` 归一化后仍保留 2.0 字段（否则解析层算得再对也白搭）
2. `l3_adjudicate._store_memory` 把这些字段以 `extra=` 形式交给 insert_memory
"""

from __future__ import annotations

import sqlite3

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao
from sgme.raw import store as raw_store
from sgme.engine import l2_extraction, l3_adjudicate, refine

CASE_FIELDS = {
    "type": "约束",
    "subject": "self",
    "certainty": "confirmed",
    "derivation": "fact",
    "modality": "done",
    "route": "memory",
    "feeling": "平静",
    "trait": "谨慎",
    "scene_key": "整理文件",
    "needs_review": 0,
    "l2_facts_json": '{"facts": [], "causes": []}',
}


@pytest.fixture
def conns(tmp_path, monkeypatch):
    cfg = sgme_config.load_config()
    monkeypatch.setattr(sgme_config, "RAW_DIR", tmp_path / "raw")
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    yield cfg, mem, sess
    for c in (mem, sess, wiki):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _seed(sess: sqlite3.Connection, file_id: str = "f-t319") -> None:
    msgs = [
        {"timestamp": "2026-10-31T00:00:01Z", "role": "user", "content": "删除操作以后都走回收站"},
        {"timestamp": "2026-10-31T00:00:02Z", "role": "assistant", "content": "好，记下了"},
    ]
    raw_store.write_new_file(
        file_id=file_id, session_key="t319", started_at="2026-10-31T00:00:00Z",
        agent_id=None, source_type="session", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        sess, file_id=file_id, path=raw_store.relative_path(file_id), session_key="t319",
        started_at="2026-10-31T00:00:00Z", status="new", last_refined_seq=0,
        size=0, content_hash=None,
    )


def test_refine_normalization_keeps_2_0_fields(conns, monkeypatch):
    """归一化白名单不得吃掉 2.0 字段（曾经的坑）。"""
    cfg, mem, sess = conns
    _seed(sess)

    raw_memory = {
        "content": "用户要求删除操作一律走回收站",
        "dimensions": ["preferences"],
        "memory_type": "persona",
        "priority": 80,
        "time_velocity": "static",
        "source_message_ids": [],
        "occurred_at": None,
        "supersedes": [],
        "facts": [],
        **CASE_FIELDS,
    }
    monkeypatch.setattr(
        l2_extraction, "extract_memories",
        lambda *a, **kw: ([raw_memory], "fake-provider", {"stage": "l1_extraction", "version": "v013", "variant": None}),
    )

    result = refine.refine_file("f-t319", mem, sess, cfg)
    assert result.status == "refined", result.error
    assert result.memories, "应当产出至少一条记忆"
    m = result.memories[0]
    for key, expect in CASE_FIELDS.items():
        assert m.get(key) == expect, f"归一化丢了 {key}"


def test_store_memory_passes_extra(conns, monkeypatch):
    """L1.5 落库必须把 2.0 字段以 extra= 交给 insert_memory。"""
    cfg, mem, sess = conns
    captured: dict = {}

    def fake_insert(conn, **kwargs):
        captured.update(kwargs)
        return "mid-x"

    monkeypatch.setattr(memory_dao, "insert_memory", fake_insert)
    new_mem = {
        "content": "用户要求删除操作一律走回收站",
        "memory_type": "persona",
        "priority": 80,
        "time_velocity": "static",
        "dimension_ids": ["preferences"],
        "occurred_at": None,
        **CASE_FIELDS,
    }
    mid = l3_adjudicate._store_memory(mem, new_mem, cfg["dimensions"], source_ref=None, prompt_version="v013")
    assert mid == "mid-x"
    extra = captured.get("extra") or {}
    for key in ("type", "subject", "route", "feeling", "trait", "scene_key", "needs_review"):
        assert extra.get(key) == CASE_FIELDS[key], f"落库丢了 {key}"
