"""tests/test_truncation_metrics.py：E5 截断计量（2026-11-05）。

背景（B312 待办 3）：127 次 L1 输出被 max_tokens 截断（completion_tokens 顶格），
但调用链把 choices[0].finish_reason 丢弃 → 无法度量截断率、无法评估 max_tokens 是否偏紧。

本文件锁死三层：
1. llm/provider.call_openai_compatible：finish_reason 并入 usage（键名同原字段；
   缺字段记 None；(text, usage) 二元组签名不变）；
2. engine/l2_extraction：finish_reason == "length" 的块计数 +1 →
   prompt_meta["l1_truncated_chunks"] 与 refine_runs.action_counts["l1_truncated_chunks"]；
3. engine/refine：RefineResult.truncated_chunks 透传（**仅计量**，不改提交/重试）。

全部零真实网络：httpx.MockTransport / monkeypatch 桩。
"""

from __future__ import annotations

import json

import httpx
import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, session_dao
from sgme.data.refine_dao import RefineRunRecorder
from sgme.engine import l2_extraction
from sgme.engine import refine as refine_mod
from sgme.prompts import BucketCtx
from sgme.raw import store


DIMS = [
    {"id": "tech_stack", "display_name": "技术栈", "type": "static"},
    {"id": "goals", "display_name": "目标", "type": "dynamic"},
]
LLM_CFG = {"chains": {"refinement": []}, "rules": {}}

GOOD_MEM = {
    "content": "用户使用 Python 写 SGME",
    "memory_type": "persona",
    "priority": 70,
    "time_velocity": "static",
    "dimensions": ["技术栈"],
}

_NODE = {"base_url": "http://127.0.0.1:1/v1", "model": "m", "context_window": 65536}
_RULES = {"timeout_s": 10}


# ---------- fixtures ----------

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


# ---------- 工具 ----------

def _mock_client(finish_reason="stop", body: str = "ok", include_finish: bool = True):
    """构造 mock LLM 客户端：返回固定 content + usage + finish_reason。"""

    def handler(req):
        choice: dict = {"message": {"content": body}}
        if include_finish:
            choice["finish_reason"] = finish_reason
        return httpx.Response(200, json={
            "choices": [choice],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _setup_raw_file(session_conn, file_id="f-trunc"):
    msgs = [
        {"timestamp": "2026-11-05T14:00:00Z", "role": "user", "content": "我用 Python 写 SGME"},
        {"timestamp": "2026-11-05T14:01:00Z", "role": "assistant", "content": "了解"},
    ]
    store.write_new_file(
        file_id=file_id, session_key="sess_trunc",
        started_at="2026-11-05T14:00:00Z", agent_id="test",
        first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=store.relative_path(file_id),
        session_key="sess_trunc", started_at="2026-11-05T14:00:00Z",
        agent_id="test", status="new", size=store.file_size(file_id),
    )
    return file_id


# ======================================================================
# 1. provider：finish_reason 并入 usage
# ======================================================================

def test_provider_finish_reason_length_in_usage():
    """finish_reason=length 透传（截断信号可达调用方），token 三键不受影响。"""
    from sgme.llm import provider

    text, usage = provider.call_openai_compatible(
        "p", _NODE, _RULES, client=_mock_client("length"))
    assert text == "ok"
    assert usage["finish_reason"] == "length"
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 5


def test_provider_finish_reason_stop_in_usage():
    from sgme.llm import provider

    _text, usage = provider.call_openai_compatible(
        "p", _NODE, _RULES, client=_mock_client("stop"))
    assert usage["finish_reason"] == "stop"


def test_provider_finish_reason_missing_is_none():
    """字段缺失 → 记 None（显式「未知」≠「正常结束」），仍不破坏签名。"""
    from sgme.llm import provider

    _text, usage = provider.call_openai_compatible(
        "p", _NODE, _RULES, client=_mock_client(include_finish=False))
    assert "finish_reason" in usage
    assert usage["finish_reason"] is None


# ======================================================================
# 2. l2_extraction：截断块计数（meta + refine_runs）
# ======================================================================

def test_extract_memories_counts_truncated_chunk(mem_conn, cfg):
    """finish_reason=length → meta 与 refine_runs.action_counts 均计 1（正常=0）。"""
    body = json.dumps([GOOD_MEM])
    memories, _provider, meta = l2_extraction.extract_memories(
        "用户说他用 Python 写 SGME", cfg["dimensions"], cfg["llm"],
        client=_mock_client("length", body=body),
        bucket_ctx=BucketCtx(bucket_key="f-trunc-run"), mem_conn=mem_conn,
    )
    assert len(memories) == 1
    assert meta["l1_truncated_chunks"] == 1

    rows = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert len(rows) == 1
    counts = json.loads(rows[0]["action_counts"])
    assert counts["l1_truncated_chunks"] == 1
    assert counts["extracted"] == 1


def test_extract_memories_normal_chunk_not_counted(mem_conn, cfg):
    """finish_reason=stop → 计数 0（键仍存在，便于 SQL 聚合不漏行）。"""
    body = json.dumps([GOOD_MEM])
    _memories, _provider, meta = l2_extraction.extract_memories(
        "用户说他用 Python 写 SGME", cfg["dimensions"], cfg["llm"],
        client=_mock_client("stop", body=body),
        bucket_ctx=BucketCtx(bucket_key="f-normal-run"), mem_conn=mem_conn,
    )
    assert meta["l1_truncated_chunks"] == 0
    rows = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert json.loads(rows[0]["action_counts"])["l1_truncated_chunks"] == 0


def test_extract_memories_sums_truncated_over_chunks(monkeypatch):
    """多块：逐块合计（两块都截断 → meta=2）。"""
    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        return [GOOD_MEM], "agnes", {
            "stage": "l1_extraction", "version": "v019", "variant": None,
            "l1_truncated_chunks": 1,
        }

    monkeypatch.setattr(l2_extraction, "_extract_memories_chunk", fake_chunk)
    _memories, _provider, meta = l2_extraction.extract_memories(
        ["块一", "块二"], DIMS, LLM_CFG, chunk_retry_times=0)
    assert meta["l1_truncated_chunks"] == 2
    assert meta["chunk_total"] == 2


# ======================================================================
# 3. refine_file：RefineResult.truncated_chunks 透传（仅计量）
# ======================================================================

def test_refine_file_exposes_truncated_chunks(raw_dir, mem_conn, session_conn, cfg):
    """截断只计数：status 仍 refined、failed_chunks 仍 0、不阻断提交。"""
    fid = _setup_raw_file(session_conn)
    body = json.dumps([GOOD_MEM])
    result = refine_mod.refine_file(
        fid, mem_conn, session_conn, cfg,
        client=_mock_client("length", body=body),
    )
    assert result.status == "refined"
    assert result.truncated_chunks == 1
    assert result.failed_chunks == 0

    # 逐块 run 已落库（与 RefineResult 同源）
    rows = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert len(rows) == 1
    assert json.loads(rows[0]["action_counts"])["l1_truncated_chunks"] == 1


def test_refine_file_normal_not_truncated(raw_dir, mem_conn, session_conn, cfg):
    fid = _setup_raw_file(session_conn, file_id="f-trunc-normal")
    body = json.dumps([GOOD_MEM])
    result = refine_mod.refine_file(
        fid, mem_conn, session_conn, cfg,
        client=_mock_client("stop", body=body),
    )
    assert result.status == "refined"
    assert result.truncated_chunks == 0
