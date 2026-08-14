"""内容哈希去重测试（Task 6：架构 §9.1 Dedup——内容哈希对比识别未变原始内容并跳过）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from sgme import config as sgme_config
from sgme.engine import refine as refine_mod
from sgme.raw import store as raw_store
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def conns(tmp_path, raw_dir):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    yield mem_conn, session_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir):
    from sgme.profile import tier0 as tier0_mod

    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    from sgme.server.app import create_app

    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def _abs_raw(fid: str) -> Path:
    """raw 文件绝对路径（RAW_DIR / relative_path）。"""
    return Path(sgme_config.RAW_DIR) / raw_store.relative_path(fid)


def _file_hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_raw_file(session_conn, fid: str, msgs: list[dict], started_at: str = "2026-08-04T10:00:00Z"):
    """写 raw 文件 + raw_files 行（带 content_hash）。"""
    raw_store.write_new_file(
        file_id=fid, session_key=f"sess-{fid}", started_at=started_at,
        agent_id="test", first_messages=msgs,
    )
    path = raw_store.relative_path(fid)
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=path, session_key=f"sess-{fid}",
        started_at=started_at, agent_id="test", status="new",
        size=raw_store.file_size(fid),
        content_hash=_file_hash(_abs_raw(fid)),
    )


def test_append_stores_content_hash(client, app):
    """append 后 raw_files 行带 content_hash（SHA-256）。"""
    session_conn = app.state.session_conn
    resp = client.post("/v1/append", json={
        "session_key": "hash-append", "started_at": "2026-08-04T10:00:00Z",
        "content": "# 2026-08-04T10:00:00Z user\n哈希测试内容\n",
    }, headers={"X-API-Key": "test-agent-key"})
    assert resp.status_code == 200, resp.text
    fid = resp.json()["file_id"]
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["content_hash"], "append 后应有 content_hash"
    assert len(rf["content_hash"]) == 64  # SHA-256 hex
    # 与文件实际哈希一致
    assert rf["content_hash"] == _file_hash(_abs_raw(fid))


def test_refine_unchanged_uses_incremental(conns, monkeypatch):
    """哈希相同且无增量 → 不重复提炼（直接 refined，不调 L1）。"""
    mem_conn, session_conn = conns
    fid = "f-hash-unchanged"
    msgs = [{"timestamp": "2026-08-04T10:00:00Z", "role": "user", "content": "内容 A"}]
    _make_raw_file(session_conn, fid, msgs)
    # 模拟已提炼过：游标=1 + refined + 哈希已存
    session_dao.update_refine_cursor(session_conn, fid, 1, status="refined")

    # mock L1：若被调用则标记
    called = {"n": 0}

    def fake_extract_l1(conversation, dimensions, llm_cfg, client=None, **kwargs):
        called["n"] += 1
        return [], "mock", {"stage": "l1_extraction", "version": "working-mock", "variant": None}

    monkeypatch.setattr("sgme.engine.l1.extract_l1", fake_extract_l1)
    result = refine_mod.refine_file(fid, mem_conn, session_conn, sgme_config.load_config())
    assert result.status == "refined"
    assert called["n"] == 0, "哈希相同且无增量 → 不应调 L1"


def test_refine_modified_triggers_full(conns, monkeypatch):
    """文件被修改（哈希变化）→ 全量重提炼（游标视为 0，L1 被调用）。"""
    mem_conn, session_conn = conns
    fid = "f-hash-modified"
    msgs = [{"timestamp": "2026-08-04T10:00:00Z", "role": "user", "content": "原始内容"}]
    _make_raw_file(session_conn, fid, msgs)
    # 模拟已提炼过
    session_dao.update_refine_cursor(session_conn, fid, 1, status="refined")
    # 外部修改文件（追加一条消息）
    raw_store.append_messages(fid, [{"timestamp": "2026-08-04T11:00:00Z", "role": "user",
                                     "content": "新追加内容"}])

    called = {"n": 0}

    def fake_extract_l1(conversation, dimensions, llm_cfg, client=None, **kwargs):
        called["n"] += 1
        # v0.5: refine.py 传预分块列表（每个元素是格式化后的会话文本）
        # 全量重提炼：conversation 应含两条消息（seq 1 + seq 2）
        full_text = "".join(conversation) if isinstance(conversation, list) else conversation
        assert "原始内容" in full_text
        assert "新追加内容" in full_text
        return [], "mock", {"stage": "l1_extraction", "version": "working-mock", "variant": None}

    monkeypatch.setattr("sgme.engine.l1.extract_l1", fake_extract_l1)
    cfg = sgme_config.load_config()
    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert called["n"] == 1, "哈希变化 → 应触发全量重提炼"
    # 提炼后哈希已更新为新文件哈希
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["content_hash"] == _file_hash(_abs_raw(fid))
    assert result.new_last_refined_seq == 2  # 两条消息都提炼了
