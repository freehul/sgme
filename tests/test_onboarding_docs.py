# -*- coding: utf-8 -*-
"""T-231（v1.7.2）：/v1/onboarding/docs 在线文档端点契约。

覆盖：
1. 索引端点 200（免 Key），白名单文档在列且逐条可寻址
2. 全文端点：根文档与 prompts/ 子目录文档均 200，内容为 markdown 非空
3. 未知文档 / 路径穿越形态 → 白名单拦截（404）
4. health.onboarding 新增指针（endpoint / repo；只增不改既有键）
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import health as health_mod
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

# 白名单冻结集（与 routes_onboarding._DOCS 同步维护；少一个即漂移）
DOC_NAMES = {
    "README.md",
    "agent-onboarding.md",
    "selfcheck.md",
    "免费模型Key申请指南.md",
    "prompts/01-install.md",
    "prompts/02-connect.md",
    "prompts/03-init-agent-files.md",
    "prompts/04-daily-loop.md",
}


# ---------- fixtures（范式与 test_stall_watch 一致：三连接全非 None） ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    # 防真探 LLM（health 用例会触达）；三连接非 None 防 own_conns 回退真实数据目录
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(
        health_mod, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "mock",
            "model": "mock-model", "error": None,
        },
    )
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
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


# ---------- 用例 ----------

def test_docs_index_public_without_key(client):
    """索引免 Key 可读；白名单文档在列且逐条可寻址。"""
    r = client.get("/v1/onboarding/docs")
    assert r.status_code == 200
    body = r.json()
    names = {d["name"] for d in body["docs"]}
    assert names >= DOC_NAMES, f"白名单文档缺失: {DOC_NAMES - names}"
    assert body["count"] == len(body["docs"])
    for d in body["docs"]:
        assert d["bytes"] > 0
        assert d["path"] == f"/v1/onboarding/docs/{d['name']}"
        assert d["title"]


def test_docs_fulltext_root_and_subdir(client):
    """根文档与 prompts/ 子目录文档均免 Key 可取全文。"""
    r = client.get("/v1/onboarding/docs/agent-onboarding.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "SGME" in r.text and len(r.text) > 500

    r2 = client.get("/v1/onboarding/docs/prompts/01-install.md")
    assert r2.status_code == 200
    assert "SGME" in r2.text and len(r2.text) > 200


def test_docs_unknown_and_traversal_blocked(client):
    """未知文档与穿越形态一律被白名单拦截（404）。"""
    assert client.get("/v1/onboarding/docs/unknown.md").status_code == 404
    # 编码斜杠穿越（%2F）与点段形态（httpx 可能先归一为 /v1/pyproject.toml，同样是 404）
    assert client.get("/v1/onboarding/docs/..%2Fpyproject.toml").status_code == 404
    assert client.get("/v1/onboarding/docs/../pyproject.toml").status_code == 404


def test_health_onboarding_pointers_added(client):
    """health.onboarding 新增 endpoint / repo 指针（只增不改）。"""
    r = client.get("/v1/health")
    assert r.status_code == 200
    ob = r.json()["onboarding"]
    assert ob["endpoint"] == "/v1/onboarding/docs"
    assert ob["repo"].startswith("http")
    # 既有字段不回归
    assert ob["tool"] == "agent_onboarding"
    assert "AI-INSTALL" in ob["docs"]
