"""tests/test_routing_inbox.py：三平面分流兜底队列（v0.3 S1，2026-11-14）。

验收：
1. 建有表 + 幂等（同源同前缀的 pending 行只有一条）
2. 收尾语义：pending → routed / discarded；已处理行不重复收尾、不物理删除
3. 操作层参数校验：非法 status / limit、routed 缺 routed_to、记录不存在
4. 端点：列队列 / 指定落点 / 明确不要

设计依据：docs/plans/设计-v0.3-三平面分流定稿与实施契约.md §3.4 + §4
（先存后丢：低置信一律进 inbox，绝不静默丢弃）
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config
from sgme.data import db as db_mod
from sgme.data import routing_inbox_dao as dao
from sgme.operations import routing_inbox as ops


@pytest.fixture
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    yield conn
    conn.close()


def _mk(conn, ref="f-1", content="某段归类不了的正文", conf=0.3):
    return dao.create(
        conn, source_ref=ref, source_type="raw_file", content=content,
        route_guess="wiki_candidate", confidence=conf, reason="项目不明",
    )


# ---------------- DAO ----------------

def test_table_created(mem_conn):
    row = mem_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_inbox'"
    ).fetchone()
    assert row is not None


def test_create_and_idempotent(mem_conn):
    a = _mk(mem_conn)
    b = _mk(mem_conn)  # 同源同内容
    assert a is not None and a == b
    assert dao.count(mem_conn) == 1
    # 不同来源 → 新行
    c = _mk(mem_conn, ref="f-2")
    assert c != a and dao.count(mem_conn) == 2


def test_create_rejects_empty(mem_conn):
    assert dao.create(mem_conn, source_ref="f-x", source_type="raw_file", content="   ") is None
    assert dao.create(mem_conn, source_ref="", source_type="raw_file", content="有正文") is None


def test_resolve_routed_and_discarded(mem_conn):
    a = _mk(mem_conn, ref="f-a")
    assert dao.resolve(mem_conn, a, status="routed", routed_to="page-123", resolved_by="user")
    row = dao.get(mem_conn, a)
    assert row["status"] == "routed" and row["routed_to"] == "page-123"
    assert row["resolved_at"] and row["resolved_by"] == "user"
    # 已处理不重复收尾
    assert dao.resolve(mem_conn, a, status="discarded") is False
    # 行仍在（软收尾，可溯源）
    assert dao.get(mem_conn, a) is not None

    b = _mk(mem_conn, ref="f-b")
    assert dao.resolve(mem_conn, b, status="discarded", resolved_by="user")
    assert dao.get(mem_conn, b)["status"] == "discarded"


def test_resolve_rejects_bad_status(mem_conn):
    iid = _mk(mem_conn)
    with pytest.raises(ValueError):
        dao.resolve(mem_conn, iid, status="pending")


def test_list_and_counts(mem_conn):
    for i in range(3):
        _mk(mem_conn, ref=f"f-{i}")
    dao.resolve(mem_conn, _mk(mem_conn, ref="done"), status="discarded")
    assert dao.count(mem_conn, status="pending") == 3
    assert dao.count_all(mem_conn) == {"pending": 3, "discarded": 1}
    assert len(dao.list_items(mem_conn, limit=2)) == 2
    assert len(dao.list_items(mem_conn, limit=2, offset=2)) == 1


# ---------------- 操作层 ----------------

def test_list_inbox_operation_ok(mem_conn):
    _mk(mem_conn)
    res = ops.list_inbox(mem_conn, status="pending", limit=10)
    assert res.ok is True
    data = res.data
    assert data["count"] == 1 and data["total"] == 1
    assert data["by_status"]["pending"] == 1


def test_list_inbox_rejects_bad_status_and_limit(mem_conn):
    assert ops.list_inbox(mem_conn, status="whatever").ok is False
    assert ops.list_inbox(mem_conn, limit=0).ok is False
    assert ops.list_inbox(mem_conn, offset=-1).ok is False


def test_resolve_inbox_requires_routed_to(mem_conn):
    iid = _mk(mem_conn)
    assert ops.resolve_inbox(mem_conn, iid, action="routed").ok is False
    assert ops.resolve_inbox(mem_conn, iid, action="routed", routed_to="  ").ok is False
    assert ops.resolve_inbox(mem_conn, iid, action="routed", routed_to="memory:abc").ok is True


def test_resolve_inbox_not_found_and_double(mem_conn):
    assert ops.resolve_inbox(mem_conn, 999999, action="discarded").ok is False
    iid = _mk(mem_conn)
    assert ops.resolve_inbox(mem_conn, iid, action="discarded").ok is True
    again = ops.resolve_inbox(mem_conn, iid, action="discarded")
    assert again.ok is False


# ---------------- 端点 ----------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    mem = db_mod.connect_memory(tmp_path / "data")
    session = db_mod.connect_session(tmp_path / "data")
    wiki = db_mod.connect_wiki(tmp_path / "data")
    app = app_mod_create(
        cfg=config.load_config(), mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin", agent_key="test-agent",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    yield TestClient(app), mem
    for conn in (mem, session, wiki):
        db_mod.close(conn)


from sgme.server.app import create_app as app_mod_create  # noqa: E402


def _h():
    return {"X-API-Key": "test-admin"}


def test_endpoints_list_route_discard(api):
    client, mem = api
    iid = _mk(mem, ref="f-ep")

    r = client.get("/v1/admin/inbox", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1 and body["items"][0]["inbox_id"] == iid

    r = client.post(f"/v1/admin/inbox/{iid}/route", json={"routed_to": "page-1", "by": "leo"}, headers=_h())
    assert r.status_code == 200
    assert r.json()["status"] == "routed"

    iid2 = _mk(mem, ref="f-ep2")
    r = client.post(f"/v1/admin/inbox/{iid2}/discard", json={"by": "leo"}, headers=_h())
    assert r.status_code == 200
    assert r.json()["status"] == "discarded"
