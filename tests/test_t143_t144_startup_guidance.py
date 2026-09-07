"""tests/test_t143_t144_startup_guidance.py：T-143 / T-144 新手体验缺陷验收。

T-144：空库首次启动 health 误报提炼停摆
- check_refinement_stalled 空库 → stalled=False + state="never_refined"
- /v1/health 空库 → refinement.state="never_refined"、heartbeat_ok=True、不发 anomaly_warn

T-143①：未配 LLM key 启动静默空转
- create_app 启动时模型 Key 缺失 → sgme.server 日志打出「模型 Key 缺失告警」
  + 申请指南路径；齐全 → 零噪音

T-143②：refine/search 全链失败或空结果无引导
- refine 单文件失败且缺 Key → 响应附加 note（含申键引导）；齐全/成功不挂 note
- search 零命中 → HTTP meta.note 带可行动引导（缺 Key 时叠加申键说明）；
  有命中 → meta 保持历史两键契约不变
"""
from __future__ import annotations

import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import health as health_mod
from sgme.engine import pipeline as pipeline_mod
from sgme.engine.refine import RefineResult
from sgme.operations.health import health as health_operation
from sgme.operations.llm import model_keys_notice
from sgme.operations.search import SEARCH_EMPTY_NOTE, http_payload as search_http_payload
from sgme.operations.search import search as search_operation
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.search import init_fts, init_scenes_fts
from sgme.data.search import vector as vector_mod
from sgme.raw import store as raw_store
from sgme.server.app import create_app
from sgme.wiki.fts import init_wiki_fts

# 指南文件相对路径（告警与 note 文案须能指路）
GUIDE_MARKER = "免费模型Key申请指南"
# 启动告警前缀（app.py T-143① 固定文案）
WARN_MARKER = "模型 Key 缺失告警"

# 与 test_key_missing_guide.py 同源：隔离 sgme.yaml 默认 vector 无 key_env，
# 缺失面只含提炼链节点（agnes → siliconflow → rule）。
_VECTOR_KEY = "SILICONFLOW_API_KEY"


def _chain_key_envs(cfg) -> list[str]:
    """推导提炼链各节点 api_key_env（rule 节点无 key 语义跳过）。"""
    chains = (cfg.get("llm") or {}).get("chains") or cfg.get("chains") or {}
    envs: list[str] = []
    for node in chains.get("refinement", []):
        if node.get("provider") == "rule":
            continue
        env = node.get("api_key_env")
        if env:
            envs.append(env)
    return envs


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
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）+ FTS 虚拟表初始化（搜索必需）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    init_fts(mem_conn)
    init_scenes_fts(mem_conn)
    init_wiki_fts(wiki_conn)
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def mock_llm(monkeypatch):
    """mock LLM 探测为可用（避免实际打 127.0.0.1:1014）。"""
    monkeypatch.setattr(
        health_mod, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "lm-studio",
            "model": "mock-model", "error": None,
        },
    )


@pytest.fixture
def mock_vector(monkeypatch):
    """mock 向量 embed 为不可用（检索自动降级纯 BM25/LIKE，离线 CI 确定性）。"""
    monkeypatch.setattr(vector_mod, "embed", lambda query, cfg, client=None: None)


@pytest.fixture
def app(conns, cfg, raw_dir, mock_llm, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（复用同一批连接）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
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


@pytest.fixture
def all_keys_set(cfg, monkeypatch):
    """显式注入全部链上 Key（确定性起点）。"""
    for k in _chain_key_envs(cfg) + [_VECTOR_KEY]:
        monkeypatch.setenv(k, "test-key-value")
    yield


@pytest.fixture
def all_keys_unset(cfg, monkeypatch):
    """显式清空全部链上 Key。"""
    for k in _chain_key_envs(cfg) + [_VECTOR_KEY]:
        monkeypatch.delenv(k, raising=False)
    yield


# ---------- 工具 ----------

def _insert_raw_file(session_conn: sqlite3.Connection, file_id: str) -> None:
    """插入 raw_files 行（status=new，供 refine_trigger 存在性预检通过）。"""
    session_conn.execute(
        """
        INSERT INTO raw_files
          (file_id, path, session_key, agent_id, started_at, ended_at,
           refined_at, last_refined_seq, status, size)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (file_id, f"raw/sessions/{file_id}.md", f"sess_{file_id}", "test",
         "2026-08-04T10:00:00Z", None, None, 1, "new", 100),
    )
    session_conn.commit()


def _error_refine_result(file_id: str) -> RefineResult:
    """构造业务失败（LLM 全链降级失败）的 RefineResult。

    文案对齐 engine.l1.py 的 RefineError —— T-143② 实测场景触发路径。
    """
    return RefineResult(
        file_id=file_id,
        memories=[],
        new_last_refined_seq=0,
        status="error",
        anomaly_warn=True,
        error="LLM 全链失败: 全链降级失败 (chain=refinement, tried=agnesh, last_error=...)",
        prompt_versions={},
    )


def _insert_memory(mem_conn: sqlite3.Connection, content: str) -> str:
    """插入一条测试记忆（tech_stack 维度）。"""
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
        sources=None,
    )


# ---------- T-144：空库 health 不误报停摆 ----------

def test_engine_health_empty_db_never_refined(conns, cfg, mock_llm):
    """空库 → check_refinement_stalled 不算停摆（stalled=False + state="never_refined"）。"""
    _mem_conn, session_conn, _ = conns

    info = health_mod.check_refinement_stalled(session_conn)

    assert info["stalled"] is False
    assert info["state"] == "never_refined"
    assert info["last_refined_at"] is None
    assert info["stalled_hours"] is None
    assert info["threshold_hours"] == 24


def test_health_operation_empty_db_never_refined(conns, cfg, mock_llm):
    """operations.health：空库 → refinement.state="never_refined"、heartbeat_ok=True。"""
    mem_conn, session_conn, _ = conns

    data = health_operation(mem_conn, session_conn, cfg).data

    assert data["refinement"]["state"] == "never_refined"
    assert data["refinement"]["stalled"] is False
    assert data["refinement"]["heartbeat_ok"] is True


def test_health_endpoint_empty_db_state(client):
    """GET /v1/health 空库 → refinement.state="never_refined"，不发布 anomaly_warn。"""
    resp = client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["refinement"]["state"] == "never_refined"
    assert body["refinement"]["stalled"] is False
    assert body["refinement"]["heartbeat_ok"] is True


# ---------- T-143①：启动模型 Key 缺失告警 ----------

def _records_with_marker(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if WARN_MARKER in r.getMessage()]


def test_startup_warns_on_missing_keys(all_keys_unset, conns, cfg, raw_dir,
                                       tmp_path, monkeypatch, caplog):
    """缺 LLM key 启动 → sgme.server 告警列出缺失 Key 并指向申请指南。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    caplog.set_level(logging.WARNING, logger="sgme.server")
    mem_conn, session_conn, wiki_conn = conns

    create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
    )

    records = _records_with_marker(caplog)
    assert records, "缺 Key 启动必须打「模型 Key 缺失告警」"
    text = records[0].getMessage()
    # 列出缺哪些（链上 Key 名出现）
    for env in _chain_key_envs(cfg):
        assert env in text
    # 指向申请指南
    assert GUIDE_MARKER in text


def test_startup_silent_when_keys_set(all_keys_set, conns, cfg, raw_dir,
                                       tmp_path, monkeypatch, caplog):
    """Key 全配齐启动 → 无「模型 Key 缺失告警」（零噪音）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    caplog.set_level(logging.WARNING, logger="sgme.server")
    mem_conn, session_conn, wiki_conn = conns

    create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
    )

    assert model_keys_notice(cfg) == ""  # 前置：齐全零噪音
    assert _records_with_marker(caplog) == []


# ---------- T-143②：refine 失败响应 note 引导 ----------

def test_refine_file_error_note_attached_when_missing(conns, cfg, all_keys_unset,
                                                      monkeypatch, raw_dir):
    """单文件提炼失败（LLM 全链降级）且缺 Key → 响应附 note 申键引导。"""
    from sgme.operations.refine import refine_trigger

    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-err")
    monkeypatch.setattr(
        pipeline_mod, "refine_one",
        lambda file_id, mem, sess, c: (_error_refine_result(file_id), {}),
    )

    res = refine_trigger(mem_conn, session_conn, cfg, file_id="f-err")

    assert res.ok is True
    assert res.data["status"] == "error"
    assert "note" in res.data
    assert "申请免费 Key" in res.data["note"]
    assert GUIDE_MARKER in res.data["note"]


def test_refine_file_success_no_note(conns, cfg, all_keys_set, monkeypatch, raw_dir):
    """单文件提炼成功 → 不挂 note，键序保持 v0.6 冻结契约。"""
    from sgme.operations.refine import refine_trigger

    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-ok")
    monkeypatch.setattr(
        pipeline_mod, "refine_one",
        lambda file_id, mem, sess, c: (
            _refine_ok_result(file_id, memories=[{"content": "m1"}]),
            {},
        ),
    )

    res = refine_trigger(mem_conn, session_conn, cfg, file_id="f-ok")

    assert res.ok is True
    assert "note" not in res.data
    assert res.data["status"] == "refined"


def _refine_ok_result(file_id: str, memories: list) -> RefineResult:
    return RefineResult(
        file_id=file_id,
        memories=memories,
        new_last_refined_seq=2,
        status="refined",
        anomaly_warn=False,
        error=None,
        prompt_versions={},
    )


def test_refine_file_error_no_note_when_keys_configured(conns, cfg, all_keys_set,
                                                        monkeypatch, raw_dir):
    """缺件排除对照：Key 齐全时即使提炼失败也不挂 note（缺失才引导）。"""
    from sgme.operations.refine import refine_trigger

    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-err-set")
    monkeypatch.setattr(
        pipeline_mod, "refine_one",
        lambda file_id, mem, sess, c: (_error_refine_result(file_id), {}),
    )

    res = refine_trigger(mem_conn, session_conn, cfg, file_id="f-err-set")

    assert res.ok is True
    assert res.data["status"] == "error"
    assert "note" not in res.data


# ---------- T-143②：search 空结果 meta.note 引导 ----------

def test_search_empty_meta_note_when_missing(conns, cfg, all_keys_unset, mock_vector):
    """零命中且缺 Key → http_payload meta.note 含搜索引导 + 申键说明。"""
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    data = search_operation(
        mem_conn, session_conn, cfg, query="zzzzqqqq", scopes=["memory"],
    ).data
    body = search_http_payload(data)

    assert body["results"] == []
    assert body["meta"]["note"]
    assert "此检索无命中" in body["meta"]["note"]
    assert SEARCH_EMPTY_NOTE in body["meta"]["note"]
    assert "申请免费 Key" in body["meta"]["note"]


def test_search_empty_meta_note_always(conns, cfg, all_keys_set, mock_vector):
    """缺件排除对照：Key 齐全时零命中仍给搜索引导（不含申键说明）。"""
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    data = search_operation(
        mem_conn, session_conn, cfg, query="zzzzqqqq", scopes=["memory"],
    ).data
    body = search_http_payload(data)

    assert body["results"] == []
    assert body["meta"]["note"]
    assert "此检索无命中" in body["meta"]["note"]
    assert "申请免费 Key" not in body["meta"]["note"]


def test_search_hit_no_meta_note(conns, cfg, all_keys_set, mock_vector):
    """有命中 → meta 保持历史两键契约（routes + rrf_k），无 note。"""
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    data = search_operation(
        mem_conn, session_conn, cfg, query="Python", scopes=["memory"],
    ).data
    body = search_http_payload(data)

    assert len(body["results"]) >= 1
    assert list(body["meta"].keys()) == ["routes", "rrf_k"]
    assert "note" not in body["meta"]