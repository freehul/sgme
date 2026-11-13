# -*- coding: utf-8 -*-
"""D-26 回归：occurred_at 必须来自「会话时间」而不是「提炼落库时间」（2026-11-13）。

背景：2.0 提示词（l1_extraction v013 起）不再产出 source_message_ids，归一化层
拿不到精确映射就退回 None，落库时兜底成 created_at → 时间轴实际按提炼时间排。
修复：提炼侧按块记录「该块最大消息时间」（extract_memories 的 chunk_ts），
归一化层在无 source_message_ids 时用它兜底。

本文件锁死三件事：
1. 无 source_message_ids 时 occurred_at = 块级会话时间；
2. 有 source_message_ids 时仍以精确消息时间为准（优先级不被兜底覆盖）；
3. 端到端 refine_file：L0 消息时间是 11-05，落库时间是「现在」→ 结果必须是 11-05。
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, session_dao
from sgme.engine import l2_extraction as l2_mod
from sgme.engine import refine as refine_mod
from sgme.raw import store


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


def _mem(content="用户偏好深色主题", *, src_ids=None, chunk_ts=None):
    """构造 _validate_item 形状的记忆（2.0 字段齐全，避免下游白名单丢弃）。"""
    item = {
        "content": content,
        "dimensions": ["preferences"],
        "memory_type": "fact",
        "priority": 70,
        "time_velocity": "static",
        "source_message_ids": list(src_ids or []),
        "supersedes": [],
        "facts": [],
        "type": "偏好",
        "subject": "self",
        "certainty": "confirmed",
        "derivation": "fact",
        "modality": "done",
        "route": "memory",
        "occurred_at": None,
    }
    if chunk_ts:
        item["_chunk_ts"] = chunk_ts
    return item


def _setup_raw_file(raw_dir, session_conn, file_id="f-occ", messages=None):
    msgs = messages or [
        {"timestamp": "2026-11-05T10:00:00Z", "role": "user", "content": "我更喜欢深色主题"},
        {"timestamp": "2026-11-05T10:01:00Z", "role": "assistant", "content": "记下了"},
    ]
    store.write_new_file(
        file_id=file_id, session_key="sess_occ",
        started_at="2026-11-05T10:00:00Z", agent_id="test",
        first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=store.relative_path(file_id),
        session_key="sess_occ", started_at="2026-11-05T10:00:00Z",
        agent_id="test", status="new", size=store.file_size(file_id),
    )
    return file_id


# ---------------------------------------------------------------- 归一化层

def test_normalize_falls_back_to_chunk_ts(cfg, mem_conn):
    raw = [_mem(chunk_ts="2026-10-01T10:00:00Z")]
    normalized, _ = refine_mod.normalize_memories(
        raw, cfg["dimensions"], mem_conn, file_id="f1", seq_ts={}, sunk_refs=[],
    )
    assert normalized[0]["occurred_at"] == "2026-10-01T10:00:00Z"


def test_source_message_ids_win_over_chunk_ts(cfg, mem_conn):
    raw = [_mem(src_ids=[5], chunk_ts="2026-10-01T10:00:00Z")]
    normalized, _ = refine_mod.normalize_memories(
        raw, cfg["dimensions"], mem_conn, file_id="f1",
        seq_ts={5: "2026-09-01T08:00:00Z"}, sunk_refs=[],
    )
    assert normalized[0]["occurred_at"] == "2026-09-01T08:00:00Z"


def test_no_chunk_ts_leaves_none(cfg, mem_conn):
    """两者都缺才允许 None（落库兜底 created_at 是最后一道，不是主路径）。"""
    raw = [_mem()]
    normalized, _ = refine_mod.normalize_memories(
        raw, cfg["dimensions"], mem_conn, file_id="f1", seq_ts={}, sunk_refs=[],
    )
    assert normalized[0]["occurred_at"] is None


# ---------------------------------------------------------------- 提取层打标

def test_extract_memories_tags_each_chunk(monkeypatch, cfg):
    calls = {"n": 0}

    def fake_chunk(*args, **kwargs):
        calls["n"] += 1
        return ([_mem(f"块{calls['n']}的记忆")], "agnes",
                {"stage": "l1_extraction", "version": "v023", "variant": None})

    monkeypatch.setattr(l2_mod, "_extract_chunk_with_retry", fake_chunk)
    memories, _provider, _meta = l2_mod.extract_memories(
        ["chunk-A", "chunk-B"], cfg["dimensions"], {},
        chunk_ts=["2026-11-01T10:00:00Z", "2026-11-02T10:00:00Z"],
    )
    assert [m["_chunk_ts"] for m in memories] == [
        "2026-11-01T10:00:00Z", "2026-11-02T10:00:00Z",
    ]


# ---------------------------------------------------------------- 端到端

def test_refine_file_uses_message_time_not_wall_clock(
    raw_dir, mem_conn, session_conn, cfg, monkeypatch
):
    """L0 消息时间 11-05，提炼发生在「现在」→ occurred_at 必须是 11-05。"""
    fid = _setup_raw_file(raw_dir, session_conn, file_id="f-occ-e2e")

    def fake_chunk(*args, **kwargs):
        return ([_mem("用户更喜欢深色主题")], "agnes",
                {"stage": "l1_extraction", "version": "v023", "variant": None,
                 "chunk_failures": 0, "chunk_total": 1, "l1_truncated_chunks": 0})

    monkeypatch.setattr(l2_mod, "_extract_chunk_with_retry", fake_chunk)
    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert result.status == "refined", result.error
    assert result.memories, "未产出记忆，测试前提不成立"
    assert result.memories[0]["occurred_at"] == "2026-11-05T10:01:00Z"
