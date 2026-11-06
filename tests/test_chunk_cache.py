"""tests/test_chunk_cache.py：块级提炼缓存（D-80，2026-11-06）。

背景：2.0 上线后提炼 token 日均 3.4M → 36.1M，根因之一是「同一批块被反复重发」
（实测平均 3.7 次 / 最坏 23 轮）。块级缓存让已成功过的块在重跑时直接复用，零 LLM。

覆盖：
1. 缓存键绑定 prompt 全文 + 模型指纹（任一变化必须失效）
2. put/get/命中计数/TTL+行数淘汰
3. 同块二次提炼（cache_enabled=True）不再调用 LLM
4. cache_enabled=False 时行为与旧版一致（每次都调 LLM）
"""
from __future__ import annotations

import pytest

from sgme.data import chunk_cache_dao, db as db_mod
from sgme.engine import l2_extraction
from sgme.prompts import BucketCtx

DIMS = [{"id": "status", "display_name": "状态", "type": "dynamic", "active": 1}]
LLM_CFG = {
    "chains": {"refinement": [{"provider": "agnes", "model": "agnes-2.5-flash"}]},
    "max_tokens": 16384,
    "rules": {},
}
GOOD_MEM = {
    "content": "用户在做显存调优",
    "memory_type": "fact",
    "priority": 50,
    "time_velocity": "static",
    "dimensions": ["status"],
}


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    yield conn
    conn.close()


def test_cache_key_binds_prompt_and_model():
    a = chunk_cache_dao.cache_key("prompt-A", "model-1")
    b = chunk_cache_dao.cache_key("prompt-B", "model-1")
    c = chunk_cache_dao.cache_key("prompt-A", "model-2")
    assert a != b, "prompt 变化必须换 key（块内容/版本/维度任一变化都在 prompt 里）"
    assert a != c, "模型链变化必须换 key（D-80：缓存不得掩盖换模型）"
    assert a == chunk_cache_dao.cache_key("prompt-A", "model-1")


def test_put_get_roundtrip_and_hit_accounting(mem_conn):
    chunk_cache_dao.ensure_table(mem_conn)
    key = chunk_cache_dao.cache_key("p", "m")
    chunk_cache_dao.put(
        mem_conn, key=key, prompt_version="v019", model_fp="m",
        chunk_sha="abc123", memories=[GOOD_MEM], provider="agnes", total_tokens=1200,
    )
    hit = chunk_cache_dao.get(mem_conn, key)
    assert hit is not None
    assert hit["memories"] == [GOOD_MEM]
    assert hit["total_tokens"] == 1200

    chunk_cache_dao.record_hit(mem_conn, key, 1200)
    chunk_cache_dao.record_hit(mem_conn, key, 1200)
    row = mem_conn.execute(
        "SELECT hit_count, reused_tokens FROM refine_chunk_cache WHERE cache_key=?", (key,)
    ).fetchone()
    assert row[0] == 2
    assert row[1] == 2400


def test_put_is_idempotent(mem_conn):
    chunk_cache_dao.ensure_table(mem_conn)
    key = chunk_cache_dao.cache_key("p", "m")
    for _ in range(3):
        chunk_cache_dao.put(
            mem_conn, key=key, prompt_version="v019", model_fp="m",
            chunk_sha="abc", memories=[], provider="agnes", total_tokens=1,
        )
    rows = mem_conn.execute("SELECT COUNT(*) FROM refine_chunk_cache").fetchone()[0]
    assert rows == 1


def test_purge_removes_overflow(mem_conn):
    chunk_cache_dao.ensure_table(mem_conn)
    for i in range(5):
        chunk_cache_dao.put(
            mem_conn, key=chunk_cache_dao.cache_key(f"p{i}", "m"), prompt_version="v019",
            model_fp="m", chunk_sha=f"sha{i}", memories=[], provider="agnes", total_tokens=1,
        )
    removed = chunk_cache_dao.purge(mem_conn, ttl_days=30, max_rows=2)
    assert removed == 3
    assert mem_conn.execute("SELECT COUNT(*) FROM refine_chunk_cache").fetchone()[0] == 2


def _patch_llm(monkeypatch, calls: dict):
    def fake_call(*args, **kwargs):
        calls["n"] = calls.get("n", 0) + 1
        return "[{\"content\": \"x\"}]", "agnes", {
            "prompt_tokens": 100, "completion_tokens": 10,
            "total_tokens": 110, "finish_reason": "stop",
        }

    monkeypatch.setattr(l2_extraction.llm_chain, "call_with_fallback", fake_call)
    monkeypatch.setattr(
        l2_extraction, "_parse_extraction_output", lambda text, dims: [dict(GOOD_MEM)]
    )
    monkeypatch.setattr(l2_extraction, "_lang_mismatch", lambda conv, mems: False)


def test_second_extraction_hits_cache_without_llm(mem_conn, monkeypatch):
    calls: dict = {}
    _patch_llm(monkeypatch, calls)
    ctx = BucketCtx(bucket_key="file-1")

    first, _p1, meta1 = l2_extraction._extract_memories_chunk(
        "user: 我在调显存", DIMS, LLM_CFG, bucket_ctx=ctx, mem_conn=mem_conn,
        cache_enabled=True,
    )
    second, _p2, meta2 = l2_extraction._extract_memories_chunk(
        "user: 我在调显存", DIMS, LLM_CFG, bucket_ctx=ctx, mem_conn=mem_conn,
        cache_enabled=True,
    )

    assert calls["n"] == 1, "第二次必须走缓存，不能再调 LLM（这是本次成本修复的核心）"
    assert first == second
    assert meta2.get("cache_hit") is True
    assert meta1.get("cache_hit") is None
    # 命中仍要留痕：一条 cache_hit=1 的 refine_runs
    hits = mem_conn.execute(
        "SELECT COUNT(*) FROM refine_runs WHERE cache_hit=1"
    ).fetchone()[0]
    assert hits == 1


def test_cache_disabled_always_calls_llm(mem_conn, monkeypatch):
    calls: dict = {}
    _patch_llm(monkeypatch, calls)
    ctx = BucketCtx(bucket_key="file-2")
    for _ in range(2):
        l2_extraction._extract_memories_chunk(
            "user: 我在调显存", DIMS, LLM_CFG, bucket_ctx=ctx, mem_conn=mem_conn,
            cache_enabled=False,
        )
    assert calls["n"] == 2
    assert mem_conn.execute("SELECT COUNT(*) FROM refine_chunk_cache").fetchone()[0] == 0


def test_failed_chunk_details_collected(monkeypatch):
    """块失败必须带「块序 + 指纹 + 原文」上报——补跑队列靠它逐块重试。"""
    calls = {"n": 0}

    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise l2_extraction.RefineError("模拟限流")
        return [GOOD_MEM], "agnes", {"stage": "l1_extraction", "version": "v019", "variant": None}

    monkeypatch.setattr(l2_extraction, "_extract_memories_chunk", fake_chunk)
    memories, _p, meta = l2_extraction.extract_memories(
        ["块一", "块二"], DIMS, LLM_CFG, chunk_retry_times=0,
    )
    assert len(memories) == 1
    details = meta["chunk_failure_details"]
    assert len(details) == 1
    assert details[0]["index"] == 1
    assert details[0]["text"] == "块二"
    assert details[0]["chunk_sha"] == l2_extraction.chunk_sha("块二")
