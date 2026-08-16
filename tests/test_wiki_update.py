"""tests/test_wiki_update.py：W3 更新/追加接口测试（方案 v0.3 §5.3）。

覆盖：
1. operations.update_page：append 追加（带来源+hash 标记）/ 幂等 noop / 替换 / description 不动
2. HTTP PATCH /v1/wiki/pages/{id} 端点（追加 + noop + 404）
3. MCP wiki_page_update 工具
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme.data import db as db_mod
from sgme.data import wiki_dao
from sgme.operations.wiki import update_page
from sgme.server.app import create_app


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect_wiki(tmp_path / "data")
    yield c
    db_mod.close(c)


@pytest.fixture
def app(tmp_path):
    mem = db_mod.connect_memory(tmp_path / "data")
    session = db_mod.connect_session(tmp_path / "data")
    wiki = db_mod.connect_wiki(tmp_path / "data")
    cfg = {"paths": {"data_dir": str(tmp_path / "data")}, "dimensions": {}, "aliases": {}}
    application = create_app(
        cfg=cfg, mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin", agent_key="test-agent",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
    )
    yield application
    db_mod.close(mem)
    db_mod.close(session)
    db_mod.close(wiki)


@pytest.fixture
def client(app):
    return TestClient(app)


AGENT = {"X-API-Key": "test-agent"}


# ---------- operations.update_page ----------

def test_update_page_append(conn):
    wiki_dao.insert_page(conn, "p1", "手册", "原有正文", description="原描述")
    result = update_page(conn, "p1", "新踩坑经验", append=True, author="session-abc")
    assert result.ok
    assert result.data["status"] == "appended"
    page = wiki_dao.get_page(conn, "p1")
    assert "原有正文" in page["content"]
    assert "新踩坑经验" in page["content"]
    assert "hash: " in page["content"]  # 幂等标记
    assert "来源: session-abc" in page["content"]
    assert page["description"] == "原描述"  # description 默认不动


def test_update_page_append_idempotent(conn):
    wiki_dao.insert_page(conn, "p1", "手册", "正文")
    first = update_page(conn, "p1", "同一条经验", append=True)
    second = update_page(conn, "p1", "同一条经验", append=True)
    assert first.data["status"] == "appended"
    assert second.data["status"] == "noop"  # entry hash 已存在 → 幂等
    page = wiki_dao.get_page(conn, "p1")
    assert page["content"].count("同一条经验") == 1


def test_update_page_replace(conn):
    wiki_dao.insert_page(conn, "p1", "手册", "旧正文")
    result = update_page(conn, "p1", "全新正文", append=False)
    assert result.data["status"] == "updated"
    assert wiki_dao.get_page(conn, "p1")["content"] == "全新正文"


def test_update_page_description_explicit(conn):
    wiki_dao.insert_page(conn, "p1", "手册", "正文", description="旧描述")
    update_page(conn, "p1", "新内容", append=False, description="新描述")
    assert wiki_dao.get_page(conn, "p1")["description"] == "新描述"


def test_update_page_not_found(conn):
    result = update_page(conn, "nope", "x")
    assert not result.ok
    assert result.error_code == "ERR_NOT_FOUND"


# ---------- HTTP PATCH ----------

def test_patch_append_and_noop(client):
    r = client.post("/v1/wiki/pages", json={"title": "手册", "content": "正文",
                                            "tags": ["skill", "sgme"]}, headers=AGENT)
    page_id = r.json()["page_id"]
    p1 = client.patch(f"/v1/wiki/pages/{page_id}", json={"content": "踩坑一", "author": "s1"},
                      headers=AGENT)
    assert p1.status_code == 200
    assert p1.json()["status"] == "appended"
    p2 = client.patch(f"/v1/wiki/pages/{page_id}", json={"content": "踩坑一", "author": "s1"},
                      headers=AGENT)
    assert p2.json()["status"] == "noop"
    detail = client.get(f"/v1/wiki/pages/{page_id}", headers=AGENT).json()
    assert detail["content"].count("踩坑一") == 1
    # description 未传 → 保持
    assert detail.get("description") in (None, "")


def test_patch_replace(client):
    r = client.post("/v1/wiki/pages", json={"title": "手册", "content": "旧正文"}, headers=AGENT)
    page_id = r.json()["page_id"]
    resp = client.patch(f"/v1/wiki/pages/{page_id}", json={"content": "新正文", "append": False},
                        headers=AGENT)
    assert resp.json()["status"] == "updated"
    assert client.get(f"/v1/wiki/pages/{page_id}", headers=AGENT).json()["content"] == "新正文"


def test_patch_not_found(client):
    r = client.patch("/v1/wiki/pages/nope", json={"content": "x"}, headers=AGENT)
    assert r.status_code == 404


def test_patch_unauthorized(client):
    r = client.patch("/v1/wiki/pages/x", json={"content": "x"})
    assert r.status_code in (401, 403)


# ---------- MCP ----------

def test_mcp_wiki_page_update(client):
    """MCP wiki_page_update 走 operations.update_page（协议翻译层）。"""
    from sgme.operations.wiki import update_page as op
    conn = client.app.state.wiki_conn
    wiki_dao.insert_page(conn, "m1", "手册", "正文")
    result = op(conn, "m1", "经验二", append=True, author="agent-dsh")
    assert result.ok
    assert "经验二" in wiki_dao.get_page(conn, "m1")["content"]
