"""tests/test_graph.py：知识图谱 graph 端点测试（ST-13；T-427 场景层退役后）。

覆盖：
1. operations/graph.py：get_graph 组装 wiki 页面节点 + wiki_links（场景层已退役）
2. API：GET /v1/admin/graph 鉴权（无 Key / Agent Key → 403）、返回结构、规模参数
3. 空库：无数据时返回空 nodes/links（不抛异常）

fixture 范式参照 tests/test_demands.py。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data import wiki_dao
from sgme.operations import graph as graph_ops
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}
AGENT_HEADERS = {"X-API-Key": AGENT_KEY}

BASE = "/v1/admin/graph"


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """隔离的 memory.db / session.db / wiki.db。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def mem_conn(conns):
    return conns[0]


@pytest.fixture
def wiki_conn(conns):
    return conns[2]


@pytest.fixture
def no_bearer(monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)


@pytest.fixture
def app(cfg, conns, no_bearer, tmp_path):
    mem, session, wiki = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem,
        session_conn=session,
        wiki_conn=wiki,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def _seed_graph(wiki_conn) -> None:
    """造数据：3 个 wiki 页面 + 2 条 wiki 边（T-427：不再造场景/记忆节点）。"""
    wiki_dao.insert_page(wiki_conn, "page-001", "Python 指南", "Python 使用手册", category="skill")
    wiki_dao.insert_page(wiki_conn, "page-002", "Rust 指南", "Rust 使用手册", category="skill")
    wiki_dao.insert_page(wiki_conn, "page-003", "SGME 架构", "记忆引擎架构", category="design")
    wiki_dao.insert_link(wiki_conn, "page-001", "page-002", rel_type="references", source="auto")
    wiki_dao.insert_link(wiki_conn, "page-002", "page-003", rel_type="similar", source="auto")


# ═══════════════════════════════════════════════════
# Operations 层测试
# ═══════════════════════════════════════════════════

class TestGraphOperations:
    """operations/graph.py 组装逻辑。"""

    def test_get_graph_nodes_and_links(self, mem_conn, wiki_conn):
        """节点全为 wiki，链接全为 wiki_link（场景层已退役）。"""
        _seed_graph(wiki_conn)
        result = graph_ops.get_graph(mem_conn, wiki_conn)
        assert result.ok
        data = result.data

        nodes = data["nodes"]
        links = data["links"]
        assert {n["type"] for n in nodes} == {"wiki"}
        assert {n["id"] for n in nodes} == {"page-001", "page-002", "page-003"}
        assert data["stats"] == {"wiki": 3, "wiki_links": 2}
        assert all(l["type"] == "wiki_link" for l in links)

    def test_get_graph_empty_db(self, mem_conn, wiki_conn):
        """空库返回空 nodes/links，不抛异常。"""
        result = graph_ops.get_graph(mem_conn, wiki_conn)
        assert result.ok
        assert result.data["nodes"] == []
        assert result.data["links"] == []

    def test_get_graph_wiki_limit(self, mem_conn, wiki_conn):
        """wiki_limit 限制页面节点数量。"""
        _seed_graph(wiki_conn)
        result = graph_ops.get_graph(mem_conn, wiki_conn, wiki_limit=2)
        assert len(result.data["nodes"]) == 2

    def test_get_graph_wiki_links_invalid_target_skipped(self, mem_conn, wiki_conn):
        """wiki_links 指向不存在的页面（孤儿边）→ 跳过，不抛异常。"""
        _seed_graph(wiki_conn)
        wiki_dao.insert_link(wiki_conn, "page-001", "ghost-page", rel_type="similar")
        result = graph_ops.get_graph(mem_conn, wiki_conn)
        assert result.ok
        ids = {n["id"] for n in result.data["nodes"]}
        assert "page-001" in ids
        # 孤儿边不进入 links
        assert all(l["target"] in ids for l in result.data["links"])


# ═══════════════════════════════════════════════════
# API 层测试
# ═══════════════════════════════════════════════════

class TestGraphApi:
    """GET /v1/admin/graph。"""

    def test_requires_admin_key(self, client):
        """无 Key → 403。"""
        resp = client.get(BASE)
        assert resp.status_code == 403

    def test_agent_key_forbidden(self, client):
        """Agent Key → 403（graph 属 admin 端点）。"""
        resp = client.get(BASE, headers=AGENT_HEADERS)
        assert resp.status_code == 403

    def test_graph_returns_structure(self, client, wiki_conn):
        """带数据返回 nodes/links 结构（仅 wiki）。"""
        _seed_graph(wiki_conn)
        resp = client.get(BASE, headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 3
        assert len(data["links"]) == 2
        assert data["stats"] == {"wiki": 3, "wiki_links": 2}

    def test_graph_empty(self, client):
        """空库返回空数组。"""
        resp = client.get(BASE, headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes"] == []
        assert data["links"] == []

    def test_graph_node_fields(self, client, wiki_conn):
        """节点字段齐全（id/type/label）。"""
        _seed_graph(wiki_conn)
        resp = client.get(BASE, headers=ADMIN_HEADERS)
        for n in resp.json()["nodes"]:
            assert n["id"]
            assert n["type"] == "wiki"
            assert n["label"]
