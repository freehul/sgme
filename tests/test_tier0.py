"""T12 测试：Tier0 画像摘要生成（§8.2）。

覆盖：
- generate_summary：LLM 成功 / LLM 全挂（不抛异常）
- save_summary + load_summary：写后读 / 48h 过期 / 文件缺失
- fallback_static：静态维度 priority>=70 top 10 直出
- /v1/inject：摘要有效 present:true / 过期 present:false
- /v1/admin/tier0/refresh：成功触发 / 无 Key 403

mock LLM 用 httpx.MockTransport 注入固定文本响应。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.profile import tier0 as tier0_mod
from sgme.server.app import create_app
from sgme.data import db as db_mod, memory_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def summary_path(tmp_path, monkeypatch):
    """隔离 tier0_summary.json 路径到 tmp_path。"""
    p = tmp_path / "tier0_summary.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)
    return p


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录。"""
    from sgme.raw import store as raw_store
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def app(tmp_path, cfg, raw_dir, summary_path, monkeypatch):
    """创建隔离的 FastAPI 应用（tmp_path data/ + raw/ + tier0 路径）。

    清理 SGME_BEARER_TOKEN 环境变量，防止 test_server.py 的 bearer 测试污染。
    """
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


def _mock_llm_client(response_text: str) -> httpx.Client:
    """构造 mock httpx 客户端，返回固定 LLM 文本。"""
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_text}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_llm_unavailable_client() -> httpx.Client:
    """构造 mock httpx 客户端：连接错误（模拟 LLM 全挂）。"""
    def handler(req):
        raise httpx.ConnectError("connection refused")
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


# ---------- generate_summary ----------

def test_generate_summary_success(mem_conn, cfg):
    """mock LLM 返回 → 生成摘要字符串。"""
    # Arrange：插入一条 identity 维度高优先级记忆
    memory_dao.insert_memory(
        mem_conn, content="用户叫张三，前端工程师", memory_type="persona",
        priority=85, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    expected = "张三是一名前端工程师，注重实用与稳定。"
    cli = _mock_llm_client(expected)

    # Act
    summary = tier0_mod.generate_summary(mem_conn, cfg, client=cli)

    # Assert
    assert summary == expected


def test_generate_summary_llm_unavailable(mem_conn, cfg):
    """LLM 全挂 → 返回 None + 不抛异常。"""
    # Arrange
    memory_dao.insert_memory(
        mem_conn, content="用户叫李四", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    cli = _mock_llm_unavailable_client()

    # Act
    summary = tier0_mod.generate_summary(mem_conn, cfg, client=cli)

    # Assert
    assert summary is None


# ---------- #33 版本观测 ----------

def test_generate_summary_records_refine_run(mem_conn, cfg):
    """#33：generate_summary 成功记录 refine_run（tier0_summary / working 版本）。"""
    from sgme.data.refine_dao import RefineRunRecorder
    memory_dao.insert_memory(
        mem_conn, content="用户叫张三，前端工程师", memory_type="persona",
        priority=85, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    expected = "张三是一名前端工程师，注重实用与稳定。"
    cli = _mock_llm_client(expected)

    summary = tier0_mod.generate_summary(mem_conn, cfg, client=cli)
    assert summary == expected
    runs = RefineRunRecorder.list_by_stage(mem_conn, "tier0_summary")
    assert len(runs) == 1
    assert runs[0]["file_id"] == "tier0"
    assert runs[0]["status"] == "ok"
    assert runs[0]["version"].startswith("working-")
    assert runs[0]["variant"] is None


def test_generate_summary_llm_unavailable_records_error_run(mem_conn, cfg):
    """#33：LLM 全挂 → 记录 error refine_run（不抛异常）。"""
    from sgme.data.refine_dao import RefineRunRecorder
    memory_dao.insert_memory(
        mem_conn, content="用户叫李四", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    cli = _mock_llm_unavailable_client()

    summary = tier0_mod.generate_summary(mem_conn, cfg, client=cli)
    assert summary is None
    runs = RefineRunRecorder.list_by_stage(mem_conn, "tier0_summary")
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["error"] is not None


# ---------- save / load ----------

def test_save_and_load_summary(summary_path):
    """save 后 load 返回摘要内容。"""
    # Arrange
    text = "这是一段 Tier0 摘要。"

    # Act
    tier0_mod.save_summary(text)
    loaded = tier0_mod.load_summary()

    # Assert
    assert loaded == text
    # 文件结构含 summary + generated_at
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert data["summary"] == text
    assert "generated_at" in data


def test_load_summary_expired(tmp_path, monkeypatch):
    """48h 后 load 返回 None。"""
    # Arrange：手动写过期文件
    p = tmp_path / "tier0_summary.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)
    expired = datetime.now(timezone.utc) - timedelta(hours=49)
    data = {
        "summary": "过期摘要",
        "generated_at": expired.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # Act
    loaded = tier0_mod.load_summary()

    # Assert
    assert loaded is None


def test_load_summary_missing_file(tmp_path, monkeypatch):
    """文件不存在 → 返回 None。"""
    # Arrange
    p = tmp_path / "nonexistent_tier0.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)

    # Act
    loaded = tier0_mod.load_summary()

    # Assert
    assert loaded is None


# ---------- fallback_static ----------

def test_fallback_static(mem_conn, cfg):
    """降级返回静态维度 priority>=70 的记忆列表。"""
    # Arrange：插入混合记忆
    memory_dao.insert_memory(
        mem_conn, content="身份事实高优", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    memory_dao.insert_memory(
        mem_conn, content="身份事实低优", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    memory_dao.insert_memory(
        mem_conn, content="家庭关系高优", memory_type="persona",
        priority=75, time_velocity="static", ttl_days=None,
        dimension_ids=["family"],
    )

    # Act
    results = tier0_mod.fallback_static(mem_conn, cfg)

    # Assert：只返回 priority>=70 的
    assert len(results) == 2
    contents = {r["content"] for r in results}
    assert "身份事实高优" in contents
    assert "家庭关系高优" in contents
    assert "身份事实低优" not in contents


# ---------- /v1/inject tier0 集成 ----------

def test_inject_tier0_present(client, summary_path):
    """/v1/inject 摘要有效时 present:true。"""
    # Arrange：写一份有效摘要
    tier0_mod.save_summary("这是有效 Tier0 摘要。")

    # Act
    resp = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier0"]["present"] is True
    assert body["tier0"]["content"] == "这是有效 Tier0 摘要。"


def test_inject_tier0_fallback(client, summary_path):
    """/v1/inject 摘要过期时 present:false。"""
    # Arrange：写过期摘要（49h 前）
    expired = datetime.now(timezone.utc) - timedelta(hours=49)
    data = {
        "summary": "过期摘要",
        "generated_at": expired.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    summary_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # Act
    resp = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier0"]["present"] is False
    assert body["tier0"]["content"] is None


# ---------- /v1/admin/tier0/refresh ----------

def test_admin_tier0_refresh(client, monkeypatch):
    """POST /v1/admin/tier0/refresh 成功。"""
    # Arrange：mock generate_summary 返回固定摘要（端点级集成，LLM 逻辑由 test_generate_summary 覆盖）
    expected = "用户是名资深工程师，追求简洁稳定。"

    def fake_generate(mem_conn_arg, cfg_arg, client=None):
        return expected

    monkeypatch.setattr(tier0_mod, "generate_summary", fake_generate)

    # Act
    resp = client.post("/v1/admin/tier0/refresh", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["summary_length"] == len(expected)
    # 摘要文件已落盘
    loaded = tier0_mod.load_summary()
    assert loaded == expected


def test_admin_tier0_refresh_no_key_403(client):
    """无 Key → 403。"""
    # Act
    resp = client.post("/v1/admin/tier0/refresh")

    # Assert
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"
