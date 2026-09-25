# -*- coding: utf-8 -*-
"""T-167 / T-169 收尾测试：L1.5 全池精确查重短路 + HTTP refine/status 端点。"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db as db_mod, memory_dao
from sgme.engine.l15 import _exact_dup_check


@pytest.fixture
def mem_conn(tmp_path):
    from sgme import config as sgme_config

    cfg = sgme_config.load_config()
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn, cfg
    conn.close()


def test_exact_dup_check_hits_active_pool(mem_conn):
    """T-167：content 与 active 池任一记忆精确相同 → 命中返回既有 memory_id。"""
    conn, _ = mem_conn
    mid = memory_dao.insert_memory(
        conn, content="用户的生产 NAS 部署 SGME 于 <NAS_IP>",
        memory_type="fact", priority=70, time_velocity="static",
        ttl_days=90, dimension_ids=["tech_stack"],
    )
    assert _exact_dup_check(conn, "用户的生产 NAS 部署 SGME 于 <NAS_IP>") == mid
    assert _exact_dup_check(conn, "完全不同的内容") is None
    assert _exact_dup_check(conn, "") is None


def test_exact_dup_check_ignores_non_active(mem_conn):
    """T-167：同 content 但 status 为 expired/archived → 不算重复（可重新落库）。"""
    conn, _ = mem_conn
    mid = memory_dao.insert_memory(
        conn, content="已被出池的旧记忆内容", memory_type="fact",
        priority=70, time_velocity="static", ttl_days=90,
        dimension_ids=["tech_stack"],
    )
    conn.execute("UPDATE memories SET status='expired' WHERE memory_id=?", (mid,))
    conn.commit()
    assert _exact_dup_check(conn, "已被出池的旧记忆内容") is None


def test_persist_short_circuits_exact_duplicate(mem_conn, monkeypatch):
    """T-167 集成：persist_memories 对全池已有 content 的新记忆短路为 skip，不落库。"""
    conn, cfg = mem_conn
    from sgme.engine.l15 import resolve_conflicts

    existing = memory_dao.insert_memory(
        conn, content="重复内容：用户每天通勤 45 分钟", memory_type="fact",
        priority=70, time_velocity="static", ttl_days=90,
        dimension_ids=["tech_stack"],
    )
    before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    # mock LLM 裁决：全部判 store（LLM 看不到候选池外的重复，正是 T-167 要堵的盲区）
    import httpx as _httpx

    def handler(req):
        return _httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    result = resolve_conflicts(
        [{"content": "重复内容：用户每天通勤 45 分钟", "memory_type": "fact",
          "priority": 70, "time_velocity": "static",
          "dimension_ids": ["tech_stack"]}],
        conn, cfg, client=_httpx.Client(
            transport=_httpx.MockTransport(handler), trust_env=False),
    )
    after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert after == before, "全池重复不应新增落库"
    assert result.stored == [] and len(result.skipped) == 1
    assert memory_dao.get_memory(conn, existing) is not None


# ---------- T-169：GET /v1/admin/refine/status ----------

def test_refine_status_http_endpoint(tmp_path, monkeypatch):
    """T-169：HTTP 端点补齐（对齐 MCP refine_status）→ 200 且含关键键。"""
    from pathlib import Path as _Path

    import httpx as _httpx
    from fastapi.testclient import TestClient

    from sgme import config as sgme_config
    from sgme.data import db as db_mod
    from sgme.profile import tier0 as tier0_mod

    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    from sgme.server.app import create_app

    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn,
        wiki_conn=wiki_conn, admin_key="test-admin-key",
        agent_key="test-agent-key", agent_store_path=tmp_path / "agent_keys.json",
    )
    client = TestClient(app)
    resp = client.get("/v1/admin/refine/status", headers={"X-API-Key": "test-admin-key"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 与 MCP refine_status 同源：四计数齐备（operations.refine.refine_status）
    for k in ("pending", "completed", "failed", "total"):
        assert k in body, f"缺键 {k}: {body}"
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)
