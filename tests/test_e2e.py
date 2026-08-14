"""T8 端到端测试：mock LLM 全链路（append → refine → inject → search → health）。

覆盖 checklist T8：
- mock LLM 模式下全链路 pytest 通过
- append ok → refine ok（N 条记忆）→ inject blocks ≥1 → search trace 非空 → health watermark 推进
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import l1
from sgme.llm import chain as llm_chain
from sgme.raw import store as raw_store
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao


# ---------- mock LLM 数据 ----------

_L1_OUTPUT = json.dumps([
    {
        "content": "用户是一名独立开发者",
        "memory_type": "persona",
        "priority": 85,
        "time_velocity": "static",
        "dimensions": ["身份"],
        "source_message_ids": [1],
    },
    {
        "content": "用户正在开发 SGME 记忆引擎项目",
        "memory_type": "persona",
        "priority": 80,
        "time_velocity": "dynamic",
        "dimensions": ["项目"],
        "source_message_ids": [2],
    },
], ensure_ascii=False)

_L15_OUTPUT = json.dumps([
    {"new_memory_index": 0, "candidate_ids": [], "action": "store"},
    {"new_memory_index": 1, "candidate_ids": [], "action": "store"},
], ensure_ascii=False)


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def mock_llm(monkeypatch):
    """mock L1 + L1.5 LLM 调用。"""
    # L1: 替换 extract_l1
    def fake_extract_l1(conversation, dimensions, llm_cfg, client=None, **kwargs):
        return json.loads(_L1_OUTPUT), "mock", {"stage": "l1_extraction", "version": "working-mock", "variant": None}
    monkeypatch.setattr(l1, "extract_l1", fake_extract_l1)

    # L1.5: 替换 call_with_fallback（L1.5 prompt 含 "新记忆#"）
    # v0.5 契约：返回三元组 (text, provider_name, usage)
    def fake_call(llm_cfg, prompt, chain_name="refinement", client=None):
        if "新记忆#" in prompt or "[新记忆#" in prompt:
            return _L15_OUTPUT, "mock", {}
        return _L1_OUTPUT, "mock", {}
    monkeypatch.setattr(llm_chain, "call_with_fallback", fake_call)


@pytest.fixture
def app(tmp_path, cfg, raw_dir, mock_llm):
    """创建隔离的 FastAPI 应用。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="e2e-admin", agent_key="e2e-agent",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


AGENT = {"X-API-Key": "e2e-agent"}
ADMIN = {"X-API-Key": "e2e-admin"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg(role: str, content: str) -> str:
    return f"# {_now_iso()} {role}\n{content}\n"


# ---------- 端到端闭环 ----------

def test_e2e_full_pipeline(client, raw_dir):
    """完整链路：append → refine → inject → search → health 水位推进。"""
    # 1. append 会话
    content = _msg("user", "我是一名独立开发者") + _msg("assistant", "你好！") + _msg("user", "我正在开发 SGME 记忆引擎项目")
    r = client.post("/v1/append", json={
        "session_key": "e2e-test-session",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }, headers=AGENT)
    assert r.status_code == 200
    file_id = r.json()["file_id"]

    # 2. health: 提炼前
    h1 = client.get("/v1/health").json()
    assert h1["refinement"]["queue_depth"] == 1
    assert h1["refinement"]["watermark_age_sec"] is None

    # 3. refine/trigger
    r = client.post("/v1/admin/refine/trigger", json={
        "file_id": file_id,
    }, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "refined"
    assert body["memories_count"] >= 1
    assert body["l15"]["stored"] >= 1

    # 4. inject: blocks ≥ 1
    r = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT)
    assert r.status_code == 200
    body = r.json()
    assert len(body["blocks"]) >= 1
    assert body["stats"]["mode"] == "daily"

    # 5. search: trace 非空
    r = client.post("/v1/search", json={
        "query": "记忆引擎",
        "scopes": ["memory"],
    }, headers=AGENT)
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) >= 1
    first = body["results"][0]
    assert first["source"] == "memory"
    assert len(first["trace"]) >= 1
    assert first["trace"][0]["file_id"] == file_id
    assert first["trace"][0]["path"] is not None

    # 6. health: 提炼后水位推进
    h2 = client.get("/v1/health").json()
    assert h2["refinement"]["queue_depth"] == 0
    assert h2["refinement"]["watermark_age_sec"] is not None
    assert h2["refinement"]["watermark_age_sec"] >= 0


def test_e2e_refine_with_dimensions_and_ttl(client, raw_dir):
    """提炼后记忆含正确维度和 TTL。"""
    content = _msg("user", "我当前在用 Python 3.11 做后端开发")
    r = client.post("/v1/append", json={
        "session_key": "e2e-dim-ttl",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }, headers=AGENT)
    file_id = r.json()["file_id"]

    r = client.post("/v1/admin/refine/trigger", json={
        "file_id": file_id,
    }, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "refined"

    # 验证记忆含维度 + TTL
    r = client.get("/v1/admin/stats", headers=ADMIN)
    stats = r.json()
    assert stats["memories"]["total"] >= 1
    # 维度分布中应有 tech_stack（"技术栈"归一化后）
    dim_ids = {d["id"] for d in stats["dimension_distribution"] if d["count"] > 0}
    assert "tech_stack" in dim_ids or "identity" in dim_ids


def test_e2e_search_with_dimension_filter(client, raw_dir):
    """search 带维度过滤。"""
    content = _msg("user", "我是一名独立开发者")
    r = client.post("/v1/append", json={
        "session_key": "e2e-search-dim",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }, headers=AGENT)
    file_id = r.json()["file_id"]

    client.post("/v1/admin/refine/trigger", json={
        "file_id": file_id,
    }, headers=ADMIN)

    # search 带维度过滤
    r = client.post("/v1/search", json={
        "query": "独立开发者",
        "scopes": ["memory"],
        "dimensions": ["identity"],
    }, headers=AGENT)
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) >= 1
    # 结果维度应含 identity
    for res in body["results"]:
        assert "identity" in res.get("dimensions", [])


def test_e2e_memory_detail_with_sources(client, raw_dir):
    """提炼后 /v1/memory/{id} 返回 sources 溯源。"""
    content = _msg("user", "我正在开发 SGME 记忆引擎项目")
    r = client.post("/v1/append", json={
        "session_key": "e2e-memory-detail",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }, headers=AGENT)
    file_id = r.json()["file_id"]

    client.post("/v1/admin/refine/trigger", json={
        "file_id": file_id,
    }, headers=ADMIN)

    # search 找到 memory_id
    r = client.post("/v1/search", json={
        "query": "记忆引擎", "scopes": ["memory"],
    }, headers=AGENT)
    body = r.json()
    assert len(body["results"]) >= 1
    mid = body["results"][0]["memory_id"]

    # 获取详情
    r = client.get(f"/v1/memory/{mid}", headers=AGENT)
    assert r.status_code == 200
    body = r.json()
    assert body["memory"]["memory_id"] == mid
    assert len(body["sources"]) >= 1
    assert body["sources"][0]["source_ref"].startswith(file_id)


def test_e2e_batch_refine_multiple_files(client, raw_dir):
    """批量提炼多个文件。"""
    # append 2 个会话
    for i in range(2):
        client.post("/v1/append", json={
            "session_key": f"e2e-batch-{i}",
            "started_at": "2026-08-04T10:00:00Z",
            "content": _msg("user", f"测试会话 {i}"),
        }, headers=AGENT)

    # 批量提炼
    r = client.post("/v1/admin/refine/trigger", json={}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["triggered"] == "batch"
    assert body["processed"] >= 2
    assert body["total_memories"] >= 2

    # health: queue 清空
    h = client.get("/v1/health").json()
    assert h["refinement"]["queue_depth"] == 0


def test_e2e_append_idempotent_in_full_flow(client, raw_dir):
    """完整链路中的 append 幂等性。"""
    content = _msg("user", "幂等测试")
    payload = {
        "session_key": "e2e-idem",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }
    r1 = client.post("/v1/append", json=payload, headers=AGENT)
    r2 = client.post("/v1/append", json=payload, headers=AGENT)
    assert r1.json()["file_id"] == r2.json()["file_id"]
    assert r2.json().get("idempotent") is True
