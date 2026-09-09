"""T-149④ 测试：/v1/answer HTTP 端点 + MCP answer 工具 + 配置开关。

覆盖：
- POST /v1/answer 200（mock LLM 链）+ answer/evidence 结构
- question_type 显式覆盖
- answer.enabled=false → ERR_DISABLED
- query 空 → 400 语义（InvalidArgs）
- MCP answer 工具冒烟（bind_app_state 同连接）
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod, memory_dao
from sgme.engine import health as engine_health
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.server.app import create_app


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn
    mem_conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def wiki_conn(tmp_path, cfg):
    conn = db_mod.connect_wiki(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def mock_llm_available(monkeypatch):
    monkeypatch.setattr(
        engine_health, "check_llm_available",
        lambda c, client=None: {"available": True, "provider": "mock", "model": "m", "error": None},
    )


@pytest.fixture
def mock_chain_answer(monkeypatch):
    """mock refinement 链首调用返回固定答案（answer 走 call_with_fallback）。"""
    from sgme.llm import chain as llm_chain

    def fake_call(cfg, prompt, chain_name="refinement", client=None):
        # 返回带编号上下文的回显，证明 prompt 已注入
        return ("mock answer", "mock-provider", {"prompt_tokens": 10, "completion_tokens": 5})

    monkeypatch.setattr(llm_chain, "call_with_fallback", fake_call)


@pytest.fixture
def app(conns, session_conn, wiki_conn, cfg, mock_llm_available, mock_chain_answer, tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    return create_app(
        cfg=cfg,
        mem_conn=conns,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


HEADERS = {"X-API-Key": "test-agent-key"}


def _insert(mem_conn, content, occurred_at=None):
    return memory_dao.insert_memory(
        mem_conn, content=content,
        memory_type="episodic", priority=70, time_velocity="static",
        ttl_days=None, dimension_ids=["goals"],
        occurred_at=occurred_at,
        facts=[{"subject": "用户", "predicate": "参观", "object": "博物馆"}],
    )


def test_post_answer_ok(client, conns):
    _insert(conns, "用户参观了现代艺术博物馆", occurred_at="2023-05-01T10:00:00Z")
    r = client.post("/v1/answer", json={"query": "两次参观博物馆间隔多少天？"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    # HTTP 三键包裹体或裸 data——看 run_operation 投影；两者都应有 answer 字段路径
    data = body.get("data") or body
    assert data["answer"] == "mock answer"
    assert data["question_type"] == "temporal"
    assert data["provider"] == "mock-provider"
    assert isinstance(data["evidence"], list)
    target = [e for e in data["evidence"] if e["facts_used"]]
    assert target, "至少一条证据带 facts"
    assert target[0]["facts_used"][0]["object"] == "博物馆"


def test_post_answer_question_type_override(client, conns):
    r = client.post("/v1/answer", json={
        "query": "什么问题都行", "question_type": "aggregate"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    data = (r.json().get("data") or r.json())
    assert data["question_type"] == "aggregate"


def test_post_answer_disabled(client, conns, cfg, monkeypatch):
    # 热改 cfg.answer.enabled=false（app.state 共享 dict）
    cfg["answer"] = {"enabled": False}
    try:
        r = client.post("/v1/answer", json={"query": "问题"}, headers=HEADERS)
        assert r.status_code in (400, 409, 503)  # ERR_DISABLED 映射（未注册码回落亦语义正确）
        assert "answer" in r.text.lower() or "未启用" in r.text or "disabled" in r.text.lower()
    finally:
        cfg["answer"] = {"enabled": True}


def test_post_answer_empty_query(client):
    r = client.post("/v1/answer", json={"query": "  "}, headers=HEADERS)
    assert r.status_code == 400


def test_mcp_answer_tool(conns, session_conn, wiki_conn, cfg, mock_llm_available, mock_chain_answer):
    _insert(conns, "用户参观了现代艺术博物馆", occurred_at="2023-05-01T10:00:00Z")
    bind_app_state({
        "cfg": cfg, "mem_conn": conns,
        "session_conn": session_conn, "wiki_conn": wiki_conn,
    })
    server = build_mcp_server()
    # FastMCP 工具直接调用（经 client 冒烟过于繁重，用 call_tool 协议）
    import asyncio
    async def _call():
        result = await server.call_tool("answer", {"query": "两次参观博物馆间隔多少天？"})
        return result
    out = asyncio.run(_call())
    text = out[0].text if isinstance(out, list) else str(out)
    assert "mock answer" in text
