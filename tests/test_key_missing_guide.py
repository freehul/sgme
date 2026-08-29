"""tests/test_key_missing_guide.py：模型 Key 缺失检测引导（T-53 免费托底）。

覆盖：
1. detect_missing_model_keys：全齐 → 空；链节点缺 → refinement 项；向量缺 → vector 项；rule 跳过
2. model_keys_notice：缺失非空文案 / 齐全空串（零噪音）
3. /v1/health 响应含 model_config（missing_keys + notice）
4. inject 缺失时 stats.note 附加申请提醒 / 齐全不附加
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.operations.llm import (
    MODEL_KEY_MISSING_NOTICE,
    detect_missing_model_keys,
    model_keys_notice,
)
from sgme.operations.inject import inject as inject_operation
from sgme.operations.inject import _attach_key_missing_note
from sgme.profile import tier0 as tier0_mod
from sgme.raw import store as raw_store
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

# 链上实际引用的 Key 从加载配置动态推导（config/llm.yaml + providers.yaml 真相源）。
# 背景：zhipu 已移出降级链（2026-08-29，9af882b），当前链为
# agnes(AGNESAI_API_KEY) → siliconflow(SILICONFLOW_API_KEY) → rule(drop_batch)。
_VECTOR_KEY = "SILICONFLOW_API_KEY"


def _chain_key_envs(cfg) -> list[str]:
    """推导提炼链各节点 api_key_env（rule 节点无 key 语义跳过）。

    与 sgme.operations.llm.detect_missing_model_keys 同一数据源，
    链再调整时测试自动跟随，不再硬编码。
    """
    chains = (cfg.get("llm") or {}).get("chains") or cfg.get("chains") or {}
    envs: list[str] = []
    for node in chains.get("refinement", []):
        if node.get("provider") == "rule":
            continue
        env = node.get("api_key_env")
        if env:
            envs.append(env)
    return envs


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, raw_dir, tmp_path, monkeypatch):
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
def summary_path(tmp_path, monkeypatch):
    p = tmp_path / "tier0_summary.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)
    return p


@pytest.fixture
def all_keys_set(cfg, monkeypatch):
    """显式注入全部链上 Key（确定性起点）。"""
    for k in _chain_key_envs(cfg) + [_VECTOR_KEY]:
        monkeypatch.setenv(k, "test-key-value")
    yield


@pytest.fixture
def all_keys_unset(cfg, monkeypatch):
    for k in _chain_key_envs(cfg) + [_VECTOR_KEY]:
        monkeypatch.delenv(k, raising=False)
    yield


# ---------- 1. 检测函数 ----------

def test_detect_all_configured_empty(all_keys_set, cfg):
    assert detect_missing_model_keys(cfg) == []


def test_detect_refine_missing(all_keys_unset, cfg, monkeypatch):
    """链首节点缺 Key → 恰 1 条 refinement 缺失（当前链首 agnes → AGNESAI_API_KEY）。"""
    envs = _chain_key_envs(cfg)
    # 除链首外全部就绪
    for k in envs[1:]:
        monkeypatch.setenv(k, "x")
    missing = detect_missing_model_keys(cfg)
    assert len(missing) == 1
    assert missing[0]["purpose"] == "refinement"
    assert missing[0]["key_env"] == envs[0]
    assert missing[0]["key_env"] == "AGNESAI_API_KEY"  # 当前链首为 agnes


def test_detect_zhipu_key_no_longer_detected(all_keys_set, cfg, monkeypatch):
    """zhipu 已移出降级链（2026-08-29）：ZHIPU_API_KEY 缺失不再产生检测项。"""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    assert detect_missing_model_keys(cfg) == []


def test_detect_vector_missing(all_keys_unset, cfg, monkeypatch):
    """测试环境 sgme.yaml 被 conftest 隔离（默认 vector 无 api_key_env），
    故构造带 api_key_env 的 cfg 副本来验证 vector 缺失检测。"""
    import copy
    cfg2 = copy.deepcopy(cfg)
    cfg2.setdefault("search", {})["vector"] = {
        "enabled": True, "provider": "siliconflow",
        "model": "BAAI/bge-m3", "api_key_env": "SILICONFLOW_API_KEY",
    }
    for k in _chain_key_envs(cfg):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    missing = detect_missing_model_keys(cfg2)
    # 注：现链 siliconflow 同时是提炼链第二级与向量端，同一 Key 缺失会
    # 连带产生 refinement 项；此处聚焦 vector 项被检测到这一原意图。
    vec = [m for m in missing if m["purpose"] == "vector"]
    assert len(vec) == 1
    assert vec[0]["key_env"] == "SILICONFLOW_API_KEY"
    refine = [m for m in missing if m["purpose"] == "refinement"]
    assert all(m["key_env"] == "SILICONFLOW_API_KEY" for m in refine)


def test_detect_rule_node_skipped(all_keys_unset, cfg, monkeypatch):
    """rule drop_batch 节点无 key 语义，不参与检测。"""
    for k in _chain_key_envs(cfg) + [_VECTOR_KEY]:
        monkeypatch.setenv(k, "x")
    assert detect_missing_model_keys(cfg) == []


# ---------- 2. 提醒文案 ----------

def test_notice_nonempty_when_missing(all_keys_unset, cfg):
    """全缺 → 文案非空且列出链上 Key 名（agnes/siliconflow 两家）。

    测试环境：llm.yaml 真实（链 agnes→siliconflow→rule），sgme.yaml 隔离
    （vector 无 key_env），故缺失仅 refinement 两家。
    注：生产文案 MODEL_KEY_MISSING_NOTICE 中智谱句的去除在生产侧同步处理，
    此处只断言链上 Key 名出现，不对 ZHIPU_API_KEY 字样的有无做断言。
    """
    notice = model_keys_notice(cfg)
    assert "申请免费 Key" in notice
    assert "AGNESAI_API_KEY" in notice        # 链首 agnes
    assert "SILICONFLOW_API_KEY" in notice    # 第二级 siliconflow
    # B121：生产文案已去除智谱句——zhipu Key 不再出现在缺失提醒里
    assert "ZHIPU_API_KEY" not in notice


def test_notice_empty_when_configured(all_keys_set, cfg):
    assert model_keys_notice(cfg) == ""


# ---------- 3. health 端点 ----------

def test_health_has_model_config(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "model_config" in data
    assert "missing_keys" in data["model_config"]
    assert "notice" in data["model_config"]


# ---------- 4. inject note ----------

def _insert(mem_conn, content, dims):
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona",
        priority=90, time_velocity="static", ttl_days=None,
        dimension_ids=dims,
    )


def test_inject_note_attached_when_missing(conns, cfg, summary_path, all_keys_unset, monkeypatch):
    mem_conn, _session, _ = conns
    _insert(mem_conn, "我是测试用户", ["identity", "family"])
    tier0_mod.save_summary("画像摘要", path=summary_path)
    res = inject_operation(mem_conn, cfg, mode="daily")
    assert res.ok
    note = res.data["stats"].get("note", "")
    assert "申请免费 Key" in note


def test_inject_note_not_attached_when_configured(conns, cfg, summary_path, all_keys_set):
    mem_conn, _session, _ = conns
    _insert(mem_conn, "我是测试用户", ["identity", "family"])
    tier0_mod.save_summary("画像摘要", path=summary_path)
    res = inject_operation(mem_conn, cfg, mode="daily")
    assert res.ok
    note = res.data["stats"].get("note", "")
    assert "申请免费 Key" not in note


def test_attach_key_missing_note_pure(all_keys_set, cfg, monkeypatch):
    """纯函数：缺失时附加、齐全保持原样。"""
    resp = {"blocks": [{"items": [{"content": "x"}]}], "stats": {"note": "原有提示"}}
    _attach_key_missing_note(resp, cfg)
    assert resp["stats"]["note"] == "原有提示"  # 齐全不附加
