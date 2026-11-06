"""tests/test_chunk_replay.py：失败块补跑队列 + 提交闸（D-80，2026-11-06）。

背景：2026-11-03 的「块失败不静默丢内容」用「整份不提交」实现，代价是同一批块
每轮追加都重发（实测最坏 23 轮 / 835 次调用）。D-80 改成：

- 成功块照常落库、游标照常推进；
- 失败块入 refine_chunk_failures，由 chunk_replay 逐块补跑；
- 入队失败 / 超过上限 / 开关关闭时，回退到旧的「整份阻断」保守路径。
"""
from __future__ import annotations

import pytest

from sgme.data import chunk_failure_dao, db as db_mod
from sgme.engine import chunk_replay, pipeline, refine as refine_mod
from sgme.engine.refine import RefineResult


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    yield conn
    conn.close()


def test_enqueue_is_idempotent_and_counts_attempts(mem_conn):
    chunk_failure_dao.ensure_table(mem_conn)
    for i in range(2):
        assert chunk_failure_dao.enqueue(
            mem_conn, file_id="f1", chunk_index=3, chunk_sha="sha3",
            prompt_version="v019", chunk_text="块文本", sunk_refs_text="",
            error=f"err{i}",
        )
    assert chunk_failure_dao.count(mem_conn) == 1
    rows = chunk_failure_dao.list_pending(mem_conn, limit=10)
    assert rows[0]["attempts"] == 2
    assert rows[0]["error"] == "err1"


def test_list_pending_skips_exhausted(mem_conn):
    chunk_failure_dao.ensure_table(mem_conn)
    chunk_failure_dao.enqueue(
        mem_conn, file_id="f1", chunk_index=0, chunk_sha="s", prompt_version="v019",
        chunk_text="t", sunk_refs_text="", error="e",
    )
    row = chunk_failure_dao.list_pending(mem_conn, limit=10)[0]
    for _ in range(3):
        chunk_failure_dao.bump(mem_conn, row["failure_id"], "still failing")
    assert chunk_failure_dao.list_pending(mem_conn, limit=10, max_attempts=3) == []
    assert chunk_failure_dao.count(mem_conn) == 1  # 记录保留，运维可见缺口


def test_resolve_removes_row(mem_conn):
    chunk_failure_dao.ensure_table(mem_conn)
    chunk_failure_dao.enqueue(
        mem_conn, file_id="f1", chunk_index=0, chunk_sha="s", prompt_version="v019",
        chunk_text="t", sunk_refs_text="", error="e",
    )
    row = chunk_failure_dao.list_pending(mem_conn, limit=10)[0]
    assert chunk_failure_dao.resolve(mem_conn, row["failure_id"]) is True
    assert chunk_failure_dao.count(mem_conn) == 0


# ---------- 提交闸 ----------

def _result_with_failures(file_id="f1", failed=2, total=10):
    r = RefineResult(file_id=file_id)
    r.failed_chunks, r.chunk_total = failed, total
    r.failed_chunk_details = [
        {"index": 1, "chunk_sha": "sha1", "text": "块一", "error": "限流"},
        {"index": 4, "chunk_sha": "sha4", "text": "块四", "error": "限流"},
    ]
    return r


def test_commit_with_queue_enqueues_and_commits(mem_conn, monkeypatch):
    """有失败块时：入队 + 照常提交（游标推进），不再是整份阻断。"""
    commits = {"n": 0}
    monkeypatch.setattr(refine_mod, "commit_refine",
                        lambda *a, **k: commits.__setitem__("n", commits["n"] + 1))
    r = _result_with_failures()
    mode = pipeline.commit_with_chunk_queue(r, mem_conn, None, {})

    assert mode == "replay_committed"
    assert commits["n"] == 1, "缺口已入队，文件必须提交（否则下一轮整份重发）"
    assert r.status == "refined"
    assert chunk_failure_dao.count(mem_conn) == 2
    assert r.anomaly_warn is True


def test_commit_with_queue_blocks_when_disabled(mem_conn, monkeypatch):
    commits = {"n": 0}
    monkeypatch.setattr(refine_mod, "commit_refine",
                        lambda *a, **k: commits.__setitem__("n", commits["n"] + 1))
    r = _result_with_failures()
    mode = pipeline.commit_with_chunk_queue(
        r, mem_conn, None, {"refine": {"chunk_replay_enabled": False}},
    )
    assert mode == "blocked"
    assert commits["n"] == 0
    assert r.status == "partial"
    assert chunk_failure_dao.count(mem_conn) == 0


def test_commit_with_queue_blocks_over_limit(mem_conn, monkeypatch):
    monkeypatch.setattr(refine_mod, "commit_refine", lambda *a, **k: None)
    r = _result_with_failures(failed=5, total=10)
    mode = pipeline.commit_with_chunk_queue(
        r, mem_conn, None, {"refine": {"chunk_replay_max_failures": 3}},
    )
    assert mode == "blocked", "超过单次入队上限 → 回退整份阻断（系统性失败更可诊断）"
    assert chunk_failure_dao.count(mem_conn) == 0


def test_commit_without_queue_conn_keeps_legacy(mem_conn, monkeypatch):
    """mem_conn 缺失（单测/离线路径）→ 不能入队 → 回退旧语义，不误提交。"""
    commits = {"n": 0}
    monkeypatch.setattr(refine_mod, "commit_refine",
                        lambda *a, **k: commits.__setitem__("n", commits["n"] + 1))
    r = _result_with_failures()
    mode = pipeline.commit_with_chunk_queue(r, None, None, {})
    assert mode == "blocked"
    assert commits["n"] == 0


def test_refine_one_enqueues_failed_chunks(mem_conn, monkeypatch):
    monkeypatch.setattr(refine_mod, "refine_file",
                        lambda file_id, *a, **k: _result_with_failures(file_id))
    monkeypatch.setattr(pipeline, "persist_memories",
                        lambda *a, **k: dict(pipeline._ZERO_STATS))
    monkeypatch.setattr(pipeline, "_resolve_file_agent", lambda *a, **k: None)
    monkeypatch.setattr(refine_mod, "commit_refine", lambda *a, **k: None)

    result, _stats = pipeline.refine_one("f1", mem_conn, None, {})
    assert result.status == "refined"
    assert chunk_failure_dao.count(mem_conn) == 2


# ---------- 补跑 ----------

def test_replay_pending_resolves_block(mem_conn, monkeypatch):
    from sgme.engine import l2_extraction

    chunk_failure_dao.ensure_table(mem_conn)
    chunk_failure_dao.enqueue(
        mem_conn, file_id="f1", chunk_index=0, chunk_sha="s1", prompt_version="v019",
        chunk_text="[msg#1] 2026-11-06T10:00:00Z user:\n我在调显存", sunk_refs_text="",
        error="限流",
    )
    seen = {}

    def fake_extract(chunks, dims, llm, **kwargs):
        seen["chunks"] = chunks
        seen["cache"] = kwargs.get("cache_enabled")
        return ([{"content": "用户在做显存调优", "memory_type": "fact", "priority": 50,
                  "time_velocity": "static", "dimensions": ["status"], "source_message_ids": [1]}],
                "agnes", {})

    monkeypatch.setattr(l2_extraction, "extract_memories", fake_extract)
    monkeypatch.setattr(pipeline, "persist_memories",
                        lambda *a, **k: dict(pipeline._ZERO_STATS))
    monkeypatch.setattr(pipeline, "_resolve_file_agent", lambda *a, **k: None)

    cfg = {"dimensions": [{"id": "status", "display_name": "状态", "type": "dynamic", "active": 1}], "llm": {}}
    stats = chunk_replay.replay_pending(mem_conn, None, cfg)

    assert stats["replayed"] == 1
    assert stats["resolved"] == 1
    assert stats["pending"] == 0
    assert seen["chunks"] == ["[msg#1] 2026-11-06T10:00:00Z user:\n我在调显存"]
    assert seen["cache"] is True


def test_replay_pending_bumps_on_failure(mem_conn, monkeypatch):
    from sgme.engine import l2_extraction

    chunk_failure_dao.ensure_table(mem_conn)
    chunk_failure_dao.enqueue(
        mem_conn, file_id="f1", chunk_index=0, chunk_sha="s1", prompt_version="v019",
        chunk_text="块", sunk_refs_text="", error="限流",
    )

    def boom(*a, **k):
        raise l2_extraction.RefineError("还是失败")

    monkeypatch.setattr(l2_extraction, "extract_memories", boom)
    stats = chunk_replay.replay_pending(mem_conn, None, {"dimensions": [], "llm": {}})
    assert stats["failed"] == 1
    assert stats["resolved"] == 0
    assert chunk_failure_dao.count(mem_conn) == 1


def test_seq_ts_parsed_from_chunk_text():
    text = "[msg#3] 2026-11-06T10:00:00Z user:\nhi\n[msg#9] 2026-11-06T11:30:00Z assistant:\nok"
    ts = chunk_replay._seq_ts_from_chunk(text)
    assert ts == {3: "2026-11-06T10:00:00Z", 9: "2026-11-06T11:30:00Z"}
