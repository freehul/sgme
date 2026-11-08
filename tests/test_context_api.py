"""tests/test_context_api.py：W06 事件上下文 API/MCP 契约测试（W01 契约 §5）。

覆盖：
1. context 形状（五键）与无数据空形态（main_event/parent_memory_id=null，列表=[]）；
2. 顶层 facts 修复：2.0 从 l2_facts_json.facts 解析（此前恒空）；1.x facts_json 三元组回落；
3. 顶层 causes 新增；坏 l2 JSON 容错（不抛、返回 []）；
4. 父边：belongs_to(source='l2_extract', from=侧事实) → parent_memory_id/main_event，
   侧事实的 context.facts/causes 回流主干；system 共现 belongs_to 不算父；
5. context.edges：双向、仅 belongs_to(l2_extract)/causes(任意 source)、五键形态；
6. MCP memory_get 同步（裸对象 + context；旧字段一个不删）；
7. /search 结果补 facts/causes（含空数组），旧键集合不变；
8. parse_l2_facts_json 容错单测。

零真实 DB：全部 tmp_path 建库；边用 edge_dao.create_edge 手工合成（不依赖 backend 写入）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import edge_dao, memory_dao
from sgme.data.search import init_fts, parse_l2_facts_json, vector as vector_mod
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations.memory import (
    CONTEXT_RELATIONS,
    L2_EDGE_SOURCE,
    build_context,
    empty_context,
    get_memory,
)
from sgme.operations.search import search as search_operation
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}
CONTEXT_KEYS = {"main_event", "facts", "causes", "parent_memory_id", "edges"}
EDGE_KEYS = {"from_id", "to_id", "relation", "weight", "source"}
HTTP_GET_TOP_KEYS = ["memory", "sources", "archive_chain", "pages", "context"]

# 2.0 单元（主干记录上的 l2_facts_json）
L2_MAIN = {
    "facts": [
        {"type": "偏好", "content": "按钮统一白字配实色底", "subject": "self", "reason": "split"},
        {"type": "约束", "content": "禁用低对比方案", "subject": "self", "reason": "keep"},
    ],
    "causes": [
        {"from": "深色底黑字对比度不足", "to": "按钮统一白字配实色底"},
    ],
}


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def mock_vector(monkeypatch):
    """向量不可达 → 检索自动降级 BM25/LIKE，测试零网络。"""
    monkeypatch.setattr(vector_mod, "embed", lambda query, cfg, client=None: None)


@pytest.fixture
def conns(tmp_path, cfg, mock_vector):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    init_fts(mem_conn)
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    m, s, w = conns
    return create_app(
        cfg=cfg, mem_conn=m, session_conn=s, wiki_conn=w,
        admin_key="test-admin-key", agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mcp(conns, cfg):
    m, s, w = conns
    bind_app_state({"cfg": cfg, "mem_conn": m, "session_conn": s, "wiki_conn": w})
    return build_mcp_server()


# ---------- 工具 ----------

def _insert(
    mem_conn: sqlite3.Connection,
    mid: str,
    content: str,
    *,
    l2=None,
    facts=None,
) -> str:
    """插入一条记忆（可选 2.0 l2_facts_json / 1.x facts 三元组）。"""
    extra = {"l2_facts_json": l2} if l2 is not None else None
    return memory_dao.insert_memory(
        mem_conn,
        content=content,
        memory_type="fact",
        priority=70,
        time_velocity="dynamic",
        ttl_days=None,
        dimension_ids=["tech_stack"],
        memory_id=mid,
        facts=facts,
        extra=extra,
    )


def _call_mcp(mcp_server, name: str, args: dict) -> str:
    raw = asyncio.run(mcp_server.call_tool(name, args))
    results, _meta = raw if isinstance(raw, tuple) else (raw, None)
    return "\n".join(c.text for c in results if getattr(c, "text", None))


def _edge_set(context: dict) -> set[tuple[str, str, str]]:
    return {(e["from_id"], e["to_id"], e["relation"]) for e in context["edges"]}


# ---------- 1. context 形状 / 无数据空形态 ----------

def test_context_shape_and_empty_defaults(client, conns):
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-empty", "ContextEmptyToken 一条普通记忆")

    resp = client.get("/v1/memory/m-empty", headers=AGENT_HEADERS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_GET_TOP_KEYS
    ctx = body["context"]
    assert set(ctx.keys()) == CONTEXT_KEYS
    assert ctx == empty_context()
    # 顶层 facts/causes 无数据 → []（不报错、不是 None）
    assert body["memory"]["facts"] == []
    assert body["memory"]["causes"] == []


def test_memory_not_found_still_404(client):
    resp = client.get("/v1/memory/no-such-id", headers=AGENT_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"


# ---------- 2. 顶层 facts 修复 ----------

def test_top_level_facts_from_l2_facts_json(client, conns):
    """2.0：facts 从 l2_facts_json.facts 解析（此前恒空），reason 不进协议。"""
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-l2", "ContextL2Token 按钮白字规则", l2=L2_MAIN)

    body = client.get("/v1/memory/m-l2", headers=AGENT_HEADERS).json()

    expected = [
        {"type": "偏好", "content": "按钮统一白字配实色底", "subject": "self"},
        {"type": "约束", "content": "禁用低对比方案", "subject": "self"},
    ]
    assert body["memory"]["facts"] == expected
    assert body["context"]["facts"] == expected
    # 旧字段不删：原始列仍在记忆对象里
    assert "l2_facts_json" in body["memory"]
    assert body["memory"]["facts_json"] is None


def test_top_level_facts_falls_back_to_legacy_triples(client, conns):
    """1.x 存量（无 l2 列）：facts 仍是 facts_json 三元组，语义不变。"""
    mem_conn, _s, _w = conns
    _insert(
        mem_conn, "m-1x", "ContextLegacyToken 用户任职",
        facts=[{"subject": "用户", "predicate": "任职于", "object": "示例公司"}],
    )

    body = client.get("/v1/memory/m-1x", headers=AGENT_HEADERS).json()

    assert body["memory"]["facts"] == [
        {"subject": "用户", "predicate": "任职于", "object": "示例公司"}
    ]
    # 无 2.0 单元 → causes 为 []（纯新增键）
    assert body["memory"]["causes"] == []
    assert body["context"]["facts"] == []
    assert body["context"]["causes"] == []


# ---------- 3. causes / 坏 JSON 容错 ----------

def test_top_level_causes_from_l2_facts_json(client, conns):
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-causes", "ContextCausesToken 对比度规则", l2=L2_MAIN)

    body = client.get("/v1/memory/m-causes", headers=AGENT_HEADERS).json()

    causes = [{"from": "深色底黑字对比度不足", "to": "按钮统一白字配实色底"}]
    assert body["memory"]["causes"] == causes
    assert body["context"]["causes"] == causes


def test_bad_l2_json_is_tolerated(client, conns):
    """坏 l2_facts_json（脏数据）→ 空数组，接口不 500。"""
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-bad", "ContextBadToken 脏数据", l2="not-a-json{{")

    resp = client.get("/v1/memory/m-bad", headers=AGENT_HEADERS)

    assert resp.status_code == 200, resp.text
    assert resp.json()["memory"]["facts"] == []
    assert resp.json()["context"] == empty_context()


# ---------- 4. 父边 / 主事件 ----------

def test_parent_via_l2_belongs_to_edge(client, conns):
    """侧事实 --belongs_to(l2_extract)--> 主干 → parent/main_event + 单元事实回流。"""
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-main", "ContextMainToken 提交按钮反馈", l2=L2_MAIN)
    _insert(mem_conn, "m-side", "提交按钮黑字看不清，要求改成白色")
    edge_dao.create_edge(
        mem_conn, "m-side", "m-main", "belongs_to", weight=1.0, source=L2_EDGE_SOURCE,
    )
    mem_conn.commit()

    ctx = client.get("/v1/memory/m-side", headers=AGENT_HEADERS).json()["context"]

    assert ctx["parent_memory_id"] == "m-main"
    assert ctx["main_event"] == {
        "memory_id": "m-main", "content": "ContextMainToken 提交按钮反馈",
    }
    # 侧事实自身无 l2 → 上下文按「单元」语义回流主干 facts/causes
    assert [f["content"] for f in ctx["facts"]] == [
        "按钮统一白字配实色底", "禁用低对比方案",
    ]
    assert ctx["causes"] == [{"from": "深色底黑字对比度不足", "to": "按钮统一白字配实色底"}]
    # 归属边进 edges（五键形态、weight 原值）
    assert ctx["edges"] == [{
        "from_id": "m-side", "to_id": "m-main", "relation": "belongs_to",
        "weight": pytest.approx(1.0), "source": L2_EDGE_SOURCE,
    }]
    assert set(ctx["edges"][0].keys()) == EDGE_KEYS


def test_main_event_sees_children_but_no_parent(client, conns):
    """主干记忆：反向 belongs_to 只说明「有子事实」，不能当父边。"""
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-main2", "ContextMain2Token 事件主干", l2=L2_MAIN)
    _insert(mem_conn, "m-side2", "ContextSide2Token 侧事实")
    edge_dao.create_edge(
        mem_conn, "m-side2", "m-main2", "belongs_to", weight=1.0, source=L2_EDGE_SOURCE,
    )
    mem_conn.commit()

    ctx = client.get("/v1/memory/m-main2", headers=AGENT_HEADERS).json()["context"]

    assert ctx["parent_memory_id"] is None
    assert ctx["main_event"] is None
    # 但子事实的归属边仍在 edges 里（对端 id 可见）
    assert ("m-side2", "m-main2", "belongs_to") in _edge_set(ctx)


def test_system_belongs_to_is_not_parent(client, conns):
    """backfill 场景共现 belongs_to（source='system'）不是父子归属。"""
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-a", "ContextSysToken 同场景A")
    _insert(mem_conn, "m-b", "ContextSysToken 同场景B")
    edge_dao.create_edge(mem_conn, "m-a", "m-b", "belongs_to", weight=3.0, source="system")
    mem_conn.commit()

    ctx = client.get("/v1/memory/m-a", headers=AGENT_HEADERS).json()["context"]

    assert ctx["parent_memory_id"] is None
    assert ctx["main_event"] is None
    assert ctx["edges"] == []  # 共现边不进事件上下文面板


def test_parent_missing_row_keeps_id_without_content(client, conns):
    """父记忆行已被清理（只剩边）：保留 parent id，main_event 不猜正文。"""
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-orphan", "ContextOrphanToken 悬挂父边")
    edge_dao.create_edge(
        mem_conn, "m-orphan", "m-gone", "belongs_to", weight=1.0, source=L2_EDGE_SOURCE,
    )
    mem_conn.commit()

    ctx = client.get("/v1/memory/m-orphan", headers=AGENT_HEADERS).json()["context"]

    assert ctx["parent_memory_id"] == "m-gone"
    assert ctx["main_event"] is None


# ---------- 5. context.edges 双向 / 关系过滤 ----------

def test_context_edges_bidirectional_and_relation_filtered(client, conns):
    mem_conn, _s, _w = conns
    for mid in ("e1", "e2", "e3", "e4", "e5", "e6"):
        _insert(mem_conn, mid, f"ContextEdgeToken {mid} 内容")
    # 出边 / 入边（双向都要）
    edge_dao.create_edge(mem_conn, "e1", "e2", "causes", weight=1.0, source=L2_EDGE_SOURCE)
    edge_dao.create_edge(mem_conn, "e3", "e1", "causes", weight=0.8, source="l1_conflict")
    # 归属边（l2_extract）保留
    edge_dao.create_edge(mem_conn, "e1", "e4", "belongs_to", weight=1.0, source=L2_EDGE_SOURCE)
    # 非上下文关系（similar / contradicts / supersedes）不进 edges
    edge_dao.create_edge(mem_conn, "e1", "e5", "similar", weight=0.9, source="l1_conflict")
    edge_dao.create_edge(mem_conn, "e1", "e6", "contradicts", weight=0.9, source="l1_conflict")
    mem_conn.commit()

    ctx = client.get("/v1/memory/e1", headers=AGENT_HEADERS).json()["context"]

    assert _edge_set(ctx) == {
        ("e1", "e2", "causes"),      # 出边
        ("e3", "e1", "causes"),      # 入边（任意 source 的因果边都算）
        ("e1", "e4", "belongs_to"),  # l2_extract 归属边
    }
    assert all(set(e.keys()) == EDGE_KEYS for e in ctx["edges"])
    assert all(e["relation"] in CONTEXT_RELATIONS for e in ctx["edges"])


# ---------- 6. MCP memory_get 同步 ----------

def test_mcp_memory_get_exposes_context(mcp, conns):
    mem_conn, _s, _w = conns
    _insert(mem_conn, "m-main3", "ContextMcpMainToken 主干", l2=L2_MAIN)
    _insert(mem_conn, "m-mcp", "ContextMcpToken 侧事实")
    edge_dao.create_edge(
        mem_conn, "m-mcp", "m-main3", "belongs_to", weight=1.0, source=L2_EDGE_SOURCE,
    )
    mem_conn.commit()

    body = json.loads(_call_mcp(mcp, "memory_get", {"memory_id": "m-mcp"}))

    # 旧字段不删（裸记忆对象）
    for key in ("memory_id", "content", "memory_type", "priority", "status", "tags", "sources"):
        assert key in body
    assert body["memory_id"] == "m-mcp"
    assert "memory" not in body and "archive_chain" not in body
    # 新字段
    assert set(body["context"].keys()) == CONTEXT_KEYS
    assert body["context"]["parent_memory_id"] == "m-main3"
    assert body["context"]["main_event"]["content"] == "ContextMcpMainToken 主干"
    assert body["facts"] == []  # 侧事实自身无 l2（单元事实在 context.facts）
    assert body["causes"] == []
    # 有 l2 的主干：MCP 顶层 facts/causes 直接可读
    main_body = json.loads(_call_mcp(mcp, "memory_get", {"memory_id": "m-main3"}))
    assert [f["content"] for f in main_body["facts"]] == [
        "按钮统一白字配实色底", "禁用低对比方案",
    ]
    assert main_body["causes"] == [{"from": "深色底黑字对比度不足", "to": "按钮统一白字配实色底"}]


# ---------- 7. /search 结果 facts/causes ----------

def test_search_results_carry_facts_and_causes(conns, cfg):
    """memory 层结果补 facts/causes（2.0 l2 优先），原始列不泄漏。"""
    mem_conn, session_conn, _w = conns
    _insert(mem_conn, "s-l2", "ContextSearchToken 按钮白字规则", l2=L2_MAIN)
    _insert(mem_conn, "s-plain", "ContextSearchToken 另一条无事实记忆")

    data = search_operation(
        mem_conn, session_conn, cfg, query="ContextSearchToken", scopes=["memory"],
    ).data

    by_id = {r["memory_id"]: r for r in data["results"]}
    assert "s-l2" in by_id and "s-plain" in by_id
    assert [f["content"] for f in by_id["s-l2"]["facts"]] == [
        "按钮统一白字配实色底", "禁用低对比方案",
    ]
    assert by_id["s-l2"]["causes"] == [
        {"from": "深色底黑字对比度不足", "to": "按钮统一白字配实色底"}
    ]
    # 无数据 → 空数组，不报错
    assert by_id["s-plain"]["facts"] == []
    assert by_id["s-plain"]["causes"] == []
    # 旧字段仍在，内部列不泄漏
    for r in data["results"]:
        assert {"rank", "score", "memory_id", "content", "dimensions", "routes"} <= set(r)
        assert "facts_json" not in r and "l2_facts_json" not in r


def test_search_results_legacy_facts_unchanged(conns, cfg):
    """1.x 存量 facts 三元组透传语义不变（不因 W06 改字段）。"""
    mem_conn, session_conn, _w = conns
    _insert(
        mem_conn, "s-1x", "ContextSearch1xToken 用户任职",
        facts=[{"subject": "用户", "predicate": "任职于", "object": "示例公司"}],
    )

    data = search_operation(
        mem_conn, session_conn, cfg, query="ContextSearch1xToken", scopes=["memory"],
    ).data

    assert data["results"][0]["facts"] == [
        {"subject": "用户", "predicate": "任职于", "object": "示例公司"}
    ]
    assert data["results"][0]["causes"] == []


def test_http_search_results_carry_facts_and_causes(client, conns):
    mem_conn, _s, _w = conns
    _insert(mem_conn, "s-http", "ContextHttpSearchToken 提交按钮反馈", l2=L2_MAIN)

    resp = client.post(
        "/v1/search",
        headers=AGENT_HEADERS,
        json={"query": "ContextHttpSearchToken", "scopes": ["memory"]},
    )

    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results
    assert results[0]["memory_id"] == "s-http"
    assert results[0]["causes"] == [
        {"from": "深色底黑字对比度不足", "to": "按钮统一白字配实色底"}
    ]
    assert results[0]["facts"][0]["type"] == "偏好"
    # W06 不引入 meta 变化
    assert set(resp.json()["meta"].keys()) <= {"routes", "rrf_k", "note"}


# ---------- 8. parse_l2_facts_json 容错单测 ----------

@pytest.mark.parametrize("raw", [None, "", "null", "[]", "{}", "oops", 123])
def test_parse_l2_facts_json_empty_or_invalid(raw):
    assert parse_l2_facts_json(raw) == {"facts": [], "causes": []}


def test_parse_l2_facts_json_normalizes_and_drops_bad_items():
    raw = {
        "facts": [
            {"type": "偏好", "content": " 白字 ", "subject": " self ", "reason": "x"},
            {"type": "偏好", "content": "   "},      # 空 content → 丢弃
            {"type": "偏好", "content": "无 subject"},
            "not-a-dict",                             # 非 dict → 丢弃
        ],
        "causes": [
            {"from": "A", "to": "B"},
            {"from": "", "to": "B"},                  # 缺一端 → 丢弃
            {"from": "A", "to": "A"},
            "bad",
        ],
    }
    out = parse_l2_facts_json(raw)
    assert out["facts"] == [
        {"type": "偏好", "content": "白字", "subject": "self"},
        {"type": "偏好", "content": "无 subject", "subject": None},
    ]
    assert out["causes"] == [{"from": "A", "to": "B"}, {"from": "A", "to": "A"}]


def test_build_context_direct_use():
    """build_context 直调（operations 层单测）：空 id 不炸、返回空形态。"""
    # 用临时内存库验证空 id 分支（不依赖 HTTP）
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ctx = build_context(conn, {"memory_id": "", "l2_facts_json": None})
        assert ctx == empty_context()
    finally:
        conn.close()


def test_get_memory_operation_data_has_context(conns):
    mem_conn, _s, _w = conns
    _insert(mem_conn, "op-1", "ContextOpToken 单测")
    data = get_memory(mem_conn, "op-1").data
    assert set(data.keys()) == {"memory", "archive_chain", "pages", "context"}
    assert data["context"] == empty_context()
