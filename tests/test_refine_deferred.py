"""tests/test_refine_deferred.py：首链熔断的「延后」语义（2026-11-05）。

验收：
1. refine_file 收到 PrimaryUnavailable → status=deferred，文件保持 new、不计失败
2. pipeline.refine_one 对 deferred 跳过落库/推游标
3. batch_scan 在熔断打开时整轮暂停（scanned=0，不调 LLM）
4. batch_scan 循环内遇到 deferred 结果 → 立即收尾，不计失败、剩余文件保持 new
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, session_dao
from sgme.engine import batch_scan as batch_scan_mod
from sgme.engine import pipeline as pipeline_mod
from sgme.engine import refine as refine_mod
from sgme.llm import chain as llm_chain
from sgme.llm import provider
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


def _setup(raw_dir, session_conn, file_id="f-defer"):
    msgs = [
        {"timestamp": "2026-11-05T10:00:00Z", "role": "user", "content": "我用 Python 写 SGME"},
        {"timestamp": "2026-11-05T10:01:00Z", "role": "assistant", "content": "了解"},
    ]
    session_key = f"sess_{file_id}"  # 每文件独立 session_key（唯一约束 session_key+started_at）
    store.write_new_file(
        file_id=file_id, session_key=session_key,
        started_at="2026-11-05T10:00:00Z", agent_id="test",
        first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=store.relative_path(file_id),
        session_key=session_key, started_at="2026-11-05T10:00:00Z",
        agent_id="test", status="new", size=store.file_size(file_id),
    )
    return file_id


def test_refine_file_defers_without_counting_failure(raw_dir, mem_conn, session_conn, cfg, monkeypatch):
    fid = _setup(raw_dir, session_conn)

    def boom(*args, **kwargs):
        raise provider.PrimaryUnavailable("首链 p1 熔断中")

    monkeypatch.setattr(refine_mod.l2_extraction, "extract_memories", boom)
    res = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert res.status == "deferred" and res.deferred is True
    row = session_dao.get_raw_file(session_conn, fid)
    assert row["status"] == "new"
    assert int(row.get("fail_count") or 0) == 0
    assert not row.get("last_error")


def test_refine_file_short_circuits_when_circuit_open(raw_dir, mem_conn, session_conn, cfg, monkeypatch):
    """熔断已打开时，refine_file 直接延后：不解析、不调 LLM。"""
    fid = _setup(raw_dir, session_conn)
    monkeypatch.setattr(llm_chain, "is_primary_open", lambda *a, **k: True)

    def must_not_parse(*a, **k):
        raise AssertionError("熔断期不应解析 L0 文件")

    monkeypatch.setattr(refine_mod.store, "parse_file", must_not_parse)
    res = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert res.status == "deferred" and res.deferred is True
    row = session_dao.get_raw_file(session_conn, fid)
    assert row["status"] == "new" and int(row.get("fail_count") or 0) == 0


def test_refine_one_skips_persist_on_deferred(mem_conn, session_conn, cfg, monkeypatch):
    def fake_refine(*args, **kwargs):
        return refine_mod.RefineResult(file_id="f-defer", status="deferred", deferred=True)

    called = {"persist": 0}

    def fake_persist(*args, **kwargs):
        called["persist"] += 1
        return {}

    monkeypatch.setattr(refine_mod, "refine_file", fake_refine)
    monkeypatch.setattr(pipeline_mod, "persist_memories", fake_persist)
    res, stats = pipeline_mod.refine_one("f-defer", mem_conn, session_conn, cfg)
    assert res.deferred is True and res.status == "deferred"
    assert called["persist"] == 0
    assert int(stats.get("stored", 0) or 0) == 0


def test_batch_scan_pauses_when_guard_open(mem_conn, session_conn, cfg, monkeypatch):
    monkeypatch.setattr(llm_chain, "is_primary_open", lambda *a, **k: True)
    out = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert out["status"] == "deferred"
    assert out["scanned"] == 0 and out["refined"] == 0 and out["failed"] == 0


def test_batch_scan_breaks_on_deferred_result(raw_dir, mem_conn, session_conn, cfg, monkeypatch):
    _setup(raw_dir, session_conn, file_id="f-defer-1")
    _setup(raw_dir, session_conn, file_id="f-defer-2")
    monkeypatch.setattr(llm_chain, "is_primary_open", lambda *a, **k: False)

    def fake_refine_one(file_id, mem, sess, cfg_):
        return refine_mod.RefineResult(file_id=file_id, status="deferred", deferred=True), {}

    monkeypatch.setattr(pipeline_mod, "refine_one", fake_refine_one)
    out = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert out["refined"] == 0 and out["failed"] == 0
    for fid in ("f-defer-1", "f-defer-2"):
        row = session_dao.get_raw_file(session_conn, fid)
        assert row["status"] == "new"
        assert int(row.get("fail_count") or 0) == 0
