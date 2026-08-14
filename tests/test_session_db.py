"""session.db 测试（v0.7 阶段 1：session.db 分离 + session_dao 读写）。

验证：
1. session.db 含 raw_files + refine_cursor 两张业务表（B1）。
2. session_dao 8 个函数对 session.db 的 CRUD 正确。
3. raw_files / refine_cursor 与 memory.db、wiki.db 物理隔离（互不串库）。
"""

import pytest

from sgme.data import db as db_mod
from sgme.data import session_dao


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


def test_session_db_has_expected_tables(session_conn):
    """session.db 含 raw_files + refine_cursor（不含 memories / scenes / wiki_pages）。"""
    tables = set(db_mod.list_tables(session_conn))
    assert {"raw_files", "refine_cursor"} <= tables
    # 隔离性：session.db 不应含记忆库 / 场景库 / 维基库的业务表
    assert "memories" not in tables
    assert "scenes" not in tables
    assert "wiki_pages" not in tables
    assert "schema_versions" in tables


def test_insert_and_get_raw_file(session_conn):
    fid = session_dao.insert_raw_file(
        session_conn, "f1", "/tmp/f1.jsonl", "session:abc",
        started_at="2026-08-20T00:00:00Z", agent_id="agent-1",
        status="new", size=1024, content_hash="h1",
    )
    assert fid == "f1"
    row = session_dao.get_raw_file(session_conn, "f1")
    assert row is not None
    assert row["file_id"] == "f1"
    assert row["session_key"] == "session:abc"
    assert row["agent_id"] == "agent-1"
    assert row["status"] == "new"
    assert row["size"] == 1024
    assert row["content_hash"] == "h1"


def test_get_raw_file_missing(session_conn):
    assert session_dao.get_raw_file(session_conn, "nope") is None


def test_get_raw_file_by_session(session_conn):
    session_dao.insert_raw_file(session_conn, "f1", "/tmp/f1.jsonl", "session:abc")
    session_dao.insert_raw_file(session_conn, "f2", "/tmp/f2.jsonl", "session:abc")
    row = session_dao.get_raw_file_by_session(session_conn, "session:abc")
    assert row is not None
    assert row["session_key"] == "session:abc"


def test_insert_raw_file_idempotent(session_conn):
    """按 file_id 幂等：重复插入只更新已知列，不重复建行。"""
    session_dao.insert_raw_file(session_conn, "f1", "/tmp/f1.jsonl", "session:abc", status="new")
    session_dao.insert_raw_file(session_conn, "f1", "/tmp/f1.jsonl", "session:abc", status="refined")
    row = session_dao.get_raw_file(session_conn, "f1")
    assert row["status"] == "refined"


def test_update_refine_cursor(session_conn):
    """update_refine_cursor 改 raw_files.last_refined_seq + refined_at + status。"""
    session_dao.insert_raw_file(session_conn, "f1", "/tmp/f1.jsonl", "session:abc", status="new")
    ok = session_dao.update_refine_cursor(
        session_conn, "f1", 42, refined_at="2026-08-20T01:00:00Z", status="refined"
    )
    assert ok
    row = session_dao.get_raw_file(session_conn, "f1")
    assert row["last_refined_seq"] == 42
    assert row["refined_at"] == "2026-08-20T01:00:00Z"
    assert row["status"] == "refined"


def test_mark_status(session_conn):
    session_dao.insert_raw_file(session_conn, "f1", "/tmp/f1.jsonl", "session:abc", status="new")
    ok = session_dao.mark_status(
        session_conn, "f1", "error", ended_at="2026-08-20T02:00:00Z", size=2048
    )
    assert ok
    row = session_dao.get_raw_file(session_conn, "f1")
    assert row["status"] == "error"
    assert row["ended_at"] == "2026-08-20T02:00:00Z"
    assert row["size"] == 2048


def test_update_content_hash(session_conn):
    session_dao.insert_raw_file(session_conn, "f1", "/tmp/f1.jsonl", "session:abc", status="new")
    ok = session_dao.update_content_hash(session_conn, "f1", "h-new")
    assert ok
    assert session_dao.get_raw_file(session_conn, "f1")["content_hash"] == "h-new"


def test_list_and_count_by_status(session_conn):
    session_dao.insert_raw_file(session_conn, "f1", "/tmp/f1.jsonl", "session:abc", status="new")
    session_dao.insert_raw_file(session_conn, "f2", "/tmp/f2.jsonl", "session:def", status="new")
    session_dao.insert_raw_file(session_conn, "f3", "/tmp/f3.jsonl", "session:ghi", status="refined")
    assert session_dao.count_by_status(session_conn, "new") == 2
    assert session_dao.count_by_status(session_conn, "refined") == 1
    new_rows = session_dao.list_by_status(session_conn, "new", limit=10)
    assert {r["file_id"] for r in new_rows} == {"f1", "f2"}


def test_raw_files_isolated_from_memory_and_wiki(tmp_path):
    """raw_files 写入 session.db 后，memory.db / wiki.db 不应含该表（物理三库隔离）。"""
    session_conn = db_mod.connect_session(tmp_path)
    session_dao.insert_raw_file(session_conn, "f1", "/tmp/f1.jsonl", "session:abc")
    session_conn.close()

    mem_conn = db_mod.connect_memory(tmp_path)
    mem_tables = set(db_mod.list_tables(mem_conn))
    mem_conn.close()

    wiki_conn = db_mod.connect_wiki(tmp_path)
    wiki_tables = set(db_mod.list_tables(wiki_conn))
    wiki_conn.close()

    assert "raw_files" not in mem_tables
    assert "raw_files" not in wiki_tables
