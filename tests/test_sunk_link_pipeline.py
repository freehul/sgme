"""沉淀索引端到端：会话含 page_id → 提炼 → 落库写 memory_pages。

用桩替掉 LLM（返回固定记忆），只验证**接线**：清单抽取 → 归一化挂 _sunk_refs
→ 落库写 memory_pages。真实跑批太贵，接线错误必须靠这条用例秒级发现。
"""

from __future__ import annotations

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao
from sgme.engine import l2_extraction, pipeline
from sgme.raw import store as raw_store

PAGE_ID = "雷池-safeline-waf-部署到-vps-全记录-2026-10-26-324963fb"
PAGE_TITLE = "雷池 SafeLine WAF 部署到 VPS 全记录"


@pytest.fixture
def env(tmp_path, monkeypatch):
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


def _seed(sess, file_id: str) -> None:
    msgs = [
        {"timestamp": "2026-10-26T00:00:01Z", "role": "user",
         "content": "把雷池 WAF 的部署记录写进知识库"},
        {"timestamp": "2026-10-26T00:00:02Z", "role": "assistant",
         "content": f'已写入：{{"page_id": "{PAGE_ID}", "title": "{PAGE_TITLE}", "status": "created"}}'},
    ]
    raw_store.write_new_file(
        file_id=file_id, session_key="sunk", started_at="2026-10-26T00:00:00Z",
        agent_id=None, source_type="session", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        sess, file_id=file_id, path=raw_store.relative_path(file_id), session_key="sunk",
        started_at="2026-10-26T00:00:00Z", status="new", last_refined_seq=0,
        size=0, content_hash=None,
    )


def test_memory_gets_linked_to_sunk_page(env, monkeypatch):
    cfg, mem, sess = env
    _seed(sess, "f-sunk")
    fake_memory = {
        "content": "用户的雷池 SafeLine WAF 部署在 VPS 上，当前已全停",
        "dimensions": ["tech_stack"],
        "memory_type": "fact",
        "priority": 50,
        "time_velocity": "static",
        "source_message_ids": [],
        "occurred_at": None,
        "supersedes": [],
        "facts": [],
        "type": "事实",
        "subject": "self",
        "route": "memory",
    }
    monkeypatch.setattr(
        l2_extraction, "extract_memories",
        lambda *a, **kw: ([fake_memory], "fake", {"stage": "l1_extraction", "version": "test"}),
    )

    result, _stats = pipeline.refine_one("f-sunk", mem, sess, cfg)
    assert result.status == "refined", result.error
    assert result.memories, "至少要产出一条记忆"

    mid = result.memories[0].get("memory_id")
    assert mid, "落库后应回写 memory_id"
    links = memory_dao.get_memory_pages(mem, mid)
    assert links, "会话里出现过 page_id，记忆必须挂上知识页索引"
    assert links[0]["kind"] == "wiki"
    assert links[0]["ref_id"] == PAGE_ID
