"""tests/test_graph.py：知识图谱 graph 端点测试（ST-13）。

覆盖：
1. operations/graph.py：get_graph 组装 nodes（场景/记忆/wiki 页面）+ links（scene_memories/wiki_links）
2. API：GET /v1/admin/graph 鉴权（无 Key / Agent Key → 403）、返回结构、规模参数
3. 空库：无数据时返回空 nodes/links（不抛异常）

fixture 范式参照 tests/test_demands.py。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data import scene_dao
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


def _seed_graph(mem_conn: sqlite3.Connection, wiki_conn: sqlite3.Connection) -> None:
    """造数据：2 场景 + 3 记忆 + 2 场景-记忆边 + 2 wiki 页面 + 1 wiki 边。"""
    # 记忆
    memory_dao.insert_memory(
        mem_conn, content="Python 后端开发", memory_type="persona", priority=85,
        time_velocity="static", ttl_days=None, dimension_ids=["tech_stack", "skills"],
        memory_id="mem-001",
    )
    memory_dao.insert_memory(
        mem_conn, content="Rust 系统编程", memory_type="episodic", priority=75,
        time_velocity="static", ttl_days=None, dimension_ids=["tech_stack"],
        memory_id="mem-002",
    )
    memory_dao.insert_memory(
        mem_conn, content="妈妈生日买蛋糕", memory_type="episodic", priority=80,
        time_velocity="dynamic", ttl_days=None, dimension_ids=["family"],
        memory_id="mem-003",
    )
    # 场景 + 关联
    scene_dao.insert_scene(mem_conn, "scene-001", "技术栈", "Python 与 Rust 的技术栈")
    scene_dao.insert_scene(mem_conn, "scene-002", "家庭", "家庭事务安排")
    scene_dao.add_memory_link(mem_conn, "scene-001", "mem-001")
    scene_dao.add_memory_link(mem_conn, "scene-001", "mem-002")
    scene_dao.add_memory_link(mem_conn, "scene-002", "mem-003")
    # wiki 页面 + 关联
    wiki_dao.insert_page(wiki_conn, "page-001", "Python 指南", "Python 使用手册", category="skill")
    wiki_dao.insert_page(wiki_conn, "page-002", "Rust 指南", "Rust 使用手册", category="skill")
    wiki_dao.insert_link(wiki_conn, "page-001", "page-002", rel_type="references", source="auto")


# ═══════════════════════════════════════════════════
# Operations 层测试
# ═══════════════════════════════════════════════════

class TestGraphOperations:
    """operations/graph.py 组装逻辑。"""

    def test_get_graph_nodes_and_links(self, mem_conn, wiki_conn):
        """节点含场景/记忆/wiki，链接含 scene_memories 与 wiki_links。"""
        _seed_graph(mem_conn, wiki_conn)
        result = graph_ops.get_graph(mem_conn, wiki_conn)
        assert result.ok
        data = result.data

        nodes = data["nodes"]
        links = data["links"]

        # 节点类型齐全
        types = {n["type"] for n in nodes}
        assert "scene" in types
        assert "memory" in types
        assert "wiki" in types

        # 场景节点带标题与记忆计数
        scene_nodes = {n["id"]: n for n in nodes if n["type"] == "scene"}
        assert "scene-001" in scene_nodes
        assert scene_nodes["scene-001"]["memories_count"] == 2

        # 记忆节点带内容与维度
        mem_nodes = {n["id"]: n for n in nodes if n["type"] == "memory"}
        assert "mem-001" in mem_nodes
        assert "tech_stack" in mem_nodes["mem-001"]["dimensions"]

        # wiki 节点
        wiki_nodes = {n["id"]: n for n in nodes if n["type"] == "wiki"}
        assert "page-001" in wiki_nodes

        # 链接：scene→memory
        sm_links = [l for l in links if l["source"] == "scene-001" and l["target"] == "mem-001"]
        assert len(sm_links) == 1
        # 链接：wiki→wiki
        ww_links = [l for l in links if l["source"] == "page-001" and l["target"] == "page-002"]
        assert len(ww_links) == 1
        assert ww_links[0]["rel_type"] == "references"

    def test_get_graph_empty_db(self, mem_conn, wiki_conn):
        """空库返回空 nodes/links，不抛异常。"""
        result = graph_ops.get_graph(mem_conn, wiki_conn)
        assert result.ok
        assert result.data["nodes"] == []
        assert result.data["links"] == []

    def test_get_graph_scene_limit(self, mem_conn, wiki_conn):
        """scene_limit 限制场景节点数量。"""
        _seed_graph(mem_conn, wiki_conn)
        result = graph_ops.get_graph(mem_conn, wiki_conn, scene_limit=1)
        scenes = [n for n in result.data["nodes"] if n["type"] == "scene"]
        assert len(scenes) == 1

    def test_get_graph_wiki_links_invalid_target_skipped(self, mem_conn, wiki_conn):
        """wiki_links 指向不存在的页面（孤儿边）→ 跳过，不抛异常。"""
        _seed_graph(mem_conn, wiki_conn)
        wiki_dao.insert_link(wiki_conn, "page-001", "ghost-page", rel_type="similar")
        result = graph_ops.get_graph(mem_conn, wiki_conn)
        assert result.ok
        # ghost-page 无节点，但边仍可存在（source 有效）；不抛异常即可
        ids = {n["id"] for n in result.data["nodes"]}
        assert "page-001" in ids

    def test_get_graph_scene_memory_orphan_skipped(self, mem_conn, wiki_conn):
        """scene→memory 边指向不存在/非 active 的记忆（孤儿外键）→ 丢弃，不产生悬空边。

        回归测试：记忆被删但 scene_memories 未清理时，旧逻辑会建出 target 无节点的边，
        d3.forceLink 抛 "node not found" 导致 WebUI 图谱渲染失败。
        """
        _seed_graph(mem_conn, wiki_conn)
        # 关联一个从未插入的记忆（模拟记忆被删未清 scene_memories 的孤儿外键）
        scene_dao.add_memory_link(mem_conn, "scene-001", "mem-ghost")
        result = graph_ops.get_graph(mem_conn, wiki_conn)
        assert result.ok
        node_ids = {n["id"] for n in result.data["nodes"]}
        for l in result.data["links"]:
            assert l["source"] in node_ids, f"悬空 source: {l}"
            assert l["target"] in node_ids, f"悬空 target: {l}"
        # 孤儿边确实被丢弃（不再出现在 links 中）
        assert not any(l["target"] == "mem-ghost" for l in result.data["links"])


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

    def test_graph_returns_structure(self, client, mem_conn, wiki_conn):
        """带数据返回 nodes/links 结构。"""
        _seed_graph(mem_conn, wiki_conn)
        resp = client.get(BASE, headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "links" in data
        assert len(data["nodes"]) >= 7  # 2 场景 + 3 记忆 + 2 wiki
        assert len(data["links"]) >= 4  # 3 scene-memory + 1 wiki-link

    def test_graph_scene_limit_param(self, client, mem_conn, wiki_conn):
        """scene_limit 参数生效。"""
        _seed_graph(mem_conn, wiki_conn)
        resp = client.get(BASE, params={"scene_limit": 1}, headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        scenes = [n for n in resp.json()["nodes"] if n["type"] == "scene"]
        assert len(scenes) == 1

    def test_graph_empty(self, client):
        """空库返回空数组。"""
        resp = client.get(BASE, headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes"] == []
        assert data["links"] == []

    def test_graph_node_fields(self, client, mem_conn, wiki_conn):
        """节点字段齐全（id/type/label）。"""
        _seed_graph(mem_conn, wiki_conn)
        resp = client.get(BASE, headers=ADMIN_HEADERS)
        for n in resp.json()["nodes"]:
            assert n["id"]
            assert n["type"] in {"scene", "memory", "wiki"}
            assert n["label"]
