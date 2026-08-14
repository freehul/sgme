"""#33 测试：提示词版本管理 Admin API（/v1/admin/prompts）+ 维度刷新联动。

覆盖：
- GET  /v1/admin/prompts：列出 stage active/ab/versions
- POST /v1/admin/prompts/publish / activate / ab
- GET  /v1/admin/prompts/metrics：按 (version, variant) 分组汇总
- 鉴权：Agent Key / 无 Key → 403
- routes_registry 写库后刷新 cfg['dimensions']（#33 修复启动快照缺口）
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.prompts import PromptStore
from sgme.raw import store as raw_store
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.refine_dao import RefineRunRecorder

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

STAGE_TEXTS = {
    "tier0_summary": "你是摘要器。\n{{memories}}",
    "l1_extraction": "你是提取器。\n{{dimensions}}\n{{conversation}}",
    "l1_conflict": "你是裁决器。\n{{new_memories}}\n{{candidates}}",
    "l2_scene": "你是聚合器。\n{{new_memories}}\n{{existing_scenes}}\n{{max_scenes}}",
}


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def prompts_root(tmp_path):
    """隔离 prompts 目录（4 个工作副本 + manifest 全 @working）。"""
    root = tmp_path / "prompts"
    root.mkdir()
    for stage, text in STAGE_TEXTS.items():
        (root / f"{stage}.txt").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir, prompts_root):
    from sgme.profile import tier0 as tier0_mod
    from sgme.server.app import create_app

    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", prompts_root / "manifest.yaml")
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)

    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- list ----------

def test_prompts_list_default_working(client):
    """默认 @working：列出 4 stage，active=@working，versions 空。"""
    resp = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["stages"]) == 4
    l1 = next(s for s in body["stages"] if s["stage"] == "l1_extraction")
    assert l1["active"] == "@working"
    assert l1["ab"]["enabled"] is False
    assert l1["versions"] == []


def test_prompts_list_after_publish(client):
    """发布后 list 返回版本元数据。"""
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction", "note": "基线"},
                headers=ADMIN_HEADERS)
    body = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS).json()
    l1 = next(s for s in body["stages"] if s["stage"] == "l1_extraction")
    assert l1["versions"][0]["version"] == "v001"
    assert l1["versions"][0]["sha256"]
    assert l1["versions"][0]["note"] == "基线"


# ---------- publish ----------

def test_publish_creates_version(client, prompts_root):
    """publish 落盘版本文件 + 返回 VersionInfo。"""
    resp = client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    ver = resp.json()["version"]
    assert ver["version"] == "v001"
    assert (prompts_root / "versions" / "l1_extraction" / "v001.txt").exists()
    # 再发一次 → v002
    resp2 = client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"},
                        headers=ADMIN_HEADERS)
    assert resp2.json()["version"]["version"] == "v002"


def test_publish_unknown_stage_400(client):
    """未知 stage → 400。"""
    resp = client.post("/v1/admin/prompts/publish", json={"stage": "no_such"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_publish_missing_placeholder_400(client, prompts_root):
    """工作副本缺占位符 → 400。"""
    (prompts_root / "l1_extraction.txt").write_text("缺占位符", encoding="utf-8")
    resp = client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 400


# ---------- activate ----------

def test_activate_pinned_and_working(client):
    """activate v001 → 钉版；activate @working → 回工作副本。"""
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)
    resp = client.post("/v1/admin/prompts/activate",
                       json={"stage": "l1_extraction", "version_ref": "v001"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS).json()
    l1 = next(s for s in body["stages"] if s["stage"] == "l1_extraction")
    assert l1["active"] == "v001"

    client.post("/v1/admin/prompts/activate",
                json={"stage": "l1_extraction", "version_ref": "@working"},
                headers=ADMIN_HEADERS)
    body = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS).json()
    l1 = next(s for s in body["stages"] if s["stage"] == "l1_extraction")
    assert l1["active"] == "@working"


def test_activate_missing_version_400(client):
    """激活不存在的版本 → 400。"""
    resp = client.post("/v1/admin/prompts/activate",
                       json={"stage": "l1_extraction", "version_ref": "v999"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 400


# ---------- ab ----------

def test_ab_configure_and_disable(client):
    """配置 A/B → enabled=true + a/b/split；关闭 → enabled=false。"""
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)
    (PromptStore.PROMPTS_ROOT / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版", encoding="utf-8",
    )
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)

    resp = client.post("/v1/admin/prompts/ab",
                       json={"stage": "l1_extraction", "a": "v001", "b": "v002", "split": 0.3},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS).json()
    l1 = next(s for s in body["stages"] if s["stage"] == "l1_extraction")
    assert l1["ab"]["enabled"] is True
    assert l1["ab"]["split"] == 0.3
    assert l1["ab"]["bucket_by"] == "file_id"

    resp2 = client.post("/v1/admin/prompts/ab",
                        json={"stage": "l1_extraction", "enabled": False},
                        headers=ADMIN_HEADERS)
    assert resp2.status_code == 200
    body = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS).json()
    l1 = next(s for s in body["stages"] if s["stage"] == "l1_extraction")
    assert l1["ab"]["enabled"] is False


def test_ab_invalid_400(client):
    """文件不存在 → 400（PromptStore 语义校验）；split 越界 → 422（pydantic schema 校验）。"""
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)
    resp = client.post("/v1/admin/prompts/ab",
                       json={"stage": "l1_extraction", "a": "v001", "b": "v999", "split": 0.5},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"
    resp2 = client.post("/v1/admin/prompts/ab",
                        json={"stage": "l1_extraction", "a": "v001", "b": "v002", "split": 1.5},
                        headers=ADMIN_HEADERS)
    assert resp2.status_code == 422


# ---------- metrics ----------

def test_metrics_groups_by_version_variant(client, app):
    """metrics 按 (version, variant) 分组返回 runs/memories/avg_priority。"""
    mem_conn = app.state.mem_conn
    # 构造 2 个 run（v001/None 与 v002/A）+ 1 条带 prompt_version 的记忆
    r1 = RefineRunRecorder.start(mem_conn, "f1", "l1_extraction", "v001", None, "lm-studio", "f1")
    RefineRunRecorder.finish(mem_conn, r1, 2, {}, "ok")
    r2 = RefineRunRecorder.start(mem_conn, "f2", "l1_extraction", "v002", "A", "deepseek", "f2")
    RefineRunRecorder.finish(mem_conn, r2, 1, {"store": 1}, "ok")
    memory_dao.insert_memory(
        mem_conn, content="m", memory_type="persona", priority=90,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v001",
    )

    resp = client.get("/v1/admin/prompts/metrics", params={"stage": "l1_extraction"},
                      headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    groups = resp.json()["groups"]
    by_key = {g["version"] + (g["variant"] or ""): g for g in groups}
    assert by_key["v001"]["runs"] == 1
    assert by_key["v001"]["memories_count"] == 2
    assert by_key["v001"]["avg_priority"] == 90.0
    assert by_key["v002A"]["runs"] == 1
    assert by_key["v002A"]["action_dist"] == {"store": 1}


def test_metrics_unknown_stage_400(client):
    """未知 stage → 400。"""
    resp = client.get("/v1/admin/prompts/metrics", params={"stage": "bad"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 400


# ---------- 鉴权 ----------

def test_prompts_requires_admin(client):
    """Agent Key / 无 Key → 403。"""
    assert client.get("/v1/admin/prompts", headers=AGENT_HEADERS).status_code == 403
    assert client.get("/v1/admin/prompts").status_code == 403
    assert client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"},
                       headers=AGENT_HEADERS).status_code == 403
    assert client.get("/v1/admin/prompts/metrics", params={"stage": "l1_extraction"}).status_code == 403


# ---------- 维度刷新联动 ----------

def test_registry_write_refreshes_cfg_dimensions(client, app):
    """#33：/v1/admin/registry 写库后刷新 cfg['dimensions']（运行时新增维度即时注入）。"""
    before_ids = {d["id"] for d in app.state.cfg["dimensions"]}
    assert "live_test_dim" not in before_ids

    resp = client.post("/v1/admin/registry/dimensions", json={
        "id": "live_test_dim", "display_name": "动态测试维度", "category": "动态",
        "time_velocity": "dynamic", "ttl_days": 7,
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text

    after_ids = {d["id"] for d in app.state.cfg["dimensions"]}
    assert "live_test_dim" in after_ids

    # 停用 → 立即从 cfg['dimensions'] 消失
    client.put("/v1/admin/registry/dimensions/live_test_dim", json={"active": False},
               headers=ADMIN_HEADERS)
    after_deact_ids = {d["id"] for d in app.state.cfg["dimensions"]}
    assert "live_test_dim" not in after_deact_ids
