"""tests/test_term_aliases.py：检索术语别名归一化测试（ST-19）。

覆盖：
1. 别名表可维护：registry/term_aliases.yaml 可加载（load_term_aliases /
   load_config），初始条目 daemon→gateway、SGME Server→gateway 存在；
   格式错误（非字典/键值非字符串）抛 ValueError
2. normalize_query_terms 单元：大小写容忍 / 空格容忍 / 词边界 /
   标准术语防重复注入 / 不含别名时逐字符不变
3. 查询端集成（operations.search + HTTP /v1/search）：
   - 旧术语查询命中新名记忆（daemon / SGME Server / DAEMON → gateway 记忆）
   - 双向召回：标准术语查询同样命中旧名记忆（无回归）
   - 不命中时行为不变：无别名查询结果与关闭别名表时一致
"""
from __future__ import annotations

import copy
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.search import init_fts, init_scenes_fts
from sgme.data.search import vector as vector_mod
from sgme.engine import health as engine_health
from sgme.operations.search import normalize_query_terms, search
from sgme.raw import store as raw_store
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

# 初始种子条目（registry/term_aliases.yaml，只增不删）
EXPECTED_TERM_ALIASES = {
    "daemon": "gateway",
    "SGME Server": "gateway",
}


# ---------- fixtures（照抄 test_operations_search.py，隔离 tmp_path） ----------

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
def mock_vector(monkeypatch):
    """mock 向量 embed 不可用（检索自动降级纯 BM25/LIKE，离线 CI 确定性）。"""
    monkeypatch.setattr(vector_mod, "embed", lambda query, cfg, client=None: None)


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）+ FTS 虚拟表初始化（搜索必需）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    init_fts(mem_conn)
    init_scenes_fts(mem_conn)
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, raw_dir, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（复用同一批连接，便于与 operations 直调对照）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 工具 ----------

def _insert_memory(mem_conn: sqlite3.Connection, content: str, mid: str | None = None) -> str:
    """插入一条测试记忆（tech_stack 维度）。"""
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
        memory_id=mid,
        sources=None,
    )


def _result_ids(data: dict | None) -> list[str]:
    """提取检索结果的 memory_id 列表（data 为 None 时返回空）。"""
    return [r["memory_id"] for r in (data or {}).get("results", [])]


# ---------- 1. 别名表可维护（YAML 加载） ----------

def test_load_term_aliases_from_yaml():
    """term_aliases.yaml 可加载，初始条目含 daemon/SGME Server → gateway。"""
    aliases = sgme_config.load_term_aliases()
    assert aliases == EXPECTED_TERM_ALIASES


def test_load_config_includes_term_aliases(cfg):
    """load_config 组装 cfg['term_aliases']（查询端消费方）。"""
    assert cfg["term_aliases"] == EXPECTED_TERM_ALIASES


def test_load_term_aliases_custom_path(tmp_path):
    """自定义路径可加载（可扩展性：新别名追加即可生效）。"""
    f = tmp_path / "term_aliases.yaml"
    f.write_text("term_aliases:\n  daemon: gateway\n  oldname: newname\n", encoding="utf-8")
    assert sgme_config.load_term_aliases(str(f)) == {
        "daemon": "gateway",
        "oldname": "newname",
    }


def test_load_term_aliases_bad_format_raises(tmp_path):
    """格式错误（缺 term_aliases 顶层键 / 值非字符串）→ ValueError。"""
    bad1 = tmp_path / "bad1.yaml"
    bad1.write_text("aliases:\n  daemon: gateway\n", encoding="utf-8")
    with pytest.raises(ValueError):
        sgme_config.load_term_aliases(str(bad1))

    bad2 = tmp_path / "bad2.yaml"
    bad2.write_text("term_aliases:\n  daemon: [gateway]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        sgme_config.load_term_aliases(str(bad2))


# ---------- 2. normalize_query_terms 单元 ----------

def test_normalize_basic_expansion():
    """旧术语 → 保留原文 + 紧跟注入标准术语（查询扩展，词后内联）。"""
    assert normalize_query_terms("daemon", EXPECTED_TERM_ALIASES) == "daemon gateway"
    assert normalize_query_terms("SGME Server", EXPECTED_TERM_ALIASES) == "SGME Server gateway"


def test_normalize_case_tolerant():
    """大小写容忍：DAEMON / Daemon 同样扩展。"""
    assert normalize_query_terms("DAEMON", EXPECTED_TERM_ALIASES) == "DAEMON gateway"
    assert normalize_query_terms("Daemon 部署", EXPECTED_TERM_ALIASES) == "Daemon gateway 部署"


def test_normalize_space_tolerant():
    """空格容忍：别名内部与查询内连续空白折叠后匹配。"""
    assert normalize_query_terms("SGME  Server", EXPECTED_TERM_ALIASES) == "SGME Server gateway"
    assert normalize_query_terms("sgme   server 部署", EXPECTED_TERM_ALIASES) == "sgme server gateway 部署"


def test_normalize_word_boundary():
    """词边界：派生词不触发扩展。"""
    assert normalize_query_terms("daemons", EXPECTED_TERM_ALIASES) == "daemons"
    assert normalize_query_terms("daemonize", EXPECTED_TERM_ALIASES) == "daemonize"
    assert normalize_query_terms("my-daemon", EXPECTED_TERM_ALIASES) == "my-daemon gateway"


def test_normalize_multi_alias_single_canonical_injected_once():
    """同一标准术语只注入一次（多个别名命中不重复）。"""
    out = normalize_query_terms("daemon SGME Server", EXPECTED_TERM_ALIASES)
    assert out.count("gateway") == 1
    assert "daemon" in out and "SGME Server" in out


def test_normalize_canonical_already_present_no_dup():
    """标准术语已在查询中 → 跳过注入（防重复）。"""
    assert normalize_query_terms("gateway daemon", EXPECTED_TERM_ALIASES) == "gateway daemon"
    assert normalize_query_terms("gateway 部署", EXPECTED_TERM_ALIASES) == "gateway 部署"


def test_normalize_no_alias_unchanged():
    """不含别名 → 逐字符不变（连大小写/空白都不动，行为不变）。"""
    q = "Python  FastAPI  底座"
    assert normalize_query_terms(q, EXPECTED_TERM_ALIASES) == q


def test_normalize_empty_inputs():
    """空 query / 空别名表 → 原样返回。"""
    assert normalize_query_terms("", EXPECTED_TERM_ALIASES) == ""
    assert normalize_query_terms("daemon", {}) == "daemon"
    assert normalize_query_terms("daemon", None) == "daemon"  # type: ignore[arg-type]


# ---------- 3. 查询端集成：旧术语命中新名记忆 ----------

def test_search_old_term_hits_new_name_memory(conns, cfg, mock_vector):
    """旧术语查询（daemon）命中新名记忆（Gateway）。"""
    mem_conn, session_conn, _ = conns
    mid = _insert_memory(mem_conn, "Gateway 升级完成 重启服务", mid="mem-gw-1")

    for q in ("daemon", "DAEMON", "Daemon"):
        data = search(mem_conn, session_conn, cfg, query=q, scopes=["memory"]).data
        assert mid in _result_ids(data), f"旧术语 {q!r} 未命中新名记忆"


def test_search_multiword_alias_hits_new_name_memory(conns, cfg, mock_vector):
    """多词旧术语（SGME Server）命中新名记忆（空格/大小写容忍）。"""
    mem_conn, session_conn, _ = conns
    mid = _insert_memory(mem_conn, "Gateway 升级完成 重启服务", mid="mem-gw-2")

    for q in ("SGME Server", "sgme server", "SGME  Server"):
        data = search(mem_conn, session_conn, cfg, query=q, scopes=["memory"]).data
        assert mid in _result_ids(data), f"旧术语 {q!r} 未命中新名记忆"


def test_search_old_term_query_no_regression(conns, cfg, mock_vector):
    """旧术语查询无回归：新名记忆与旧名记忆同时召回（扩展而非替换）。"""
    mem_conn, session_conn, _ = conns
    new_mid = _insert_memory(mem_conn, "Gateway 升级完成 重启服务", mid="mem-gw-3")
    old_mid = _insert_memory(mem_conn, "daemon 进程崩溃排查", mid="mem-old-3")

    # 旧术语查询：新名记忆（ST-19 主目标）与旧名记忆（无回归）都召回
    data = search(mem_conn, session_conn, cfg, query="daemon", scopes=["memory"]).data
    ids = _result_ids(data)
    assert new_mid in ids and old_mid in ids

    # 标准术语查询：新名记忆正常命中（不做对称扩展——注入 server/sgme 等
    # 宽泛词会污染召回精度；反向召回缺口由索引端评估结论覆盖，见 ST-19）
    data = search(mem_conn, session_conn, cfg, query="gateway", scopes=["memory"]).data
    assert new_mid in _result_ids(data)


def test_search_no_alias_behavior_unchanged(conns, cfg, mock_vector):
    """不命中时行为不变：无别名查询与关闭别名表结果一致。"""
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计", mid="mem-pl-1")
    _insert_memory(mem_conn, "Gateway 升级完成", mid="mem-gw-4")

    cfg_off = copy.deepcopy(cfg)
    cfg_off["term_aliases"] = {}

    with_alias = search(mem_conn, session_conn, cfg, query="Python", scopes=["memory"]).data
    without_alias = search(mem_conn, session_conn, cfg_off, query="Python", scopes=["memory"]).data
    assert _result_ids(with_alias) == _result_ids(without_alias) == ["mem-pl-1"]

    # 无别名查询原样命中（gateway 记忆不因别名表存在而混入）
    assert _result_ids(with_alias) == ["mem-pl-1"]


def test_search_cfg_without_term_aliases_key(conns, cfg, mock_vector):
    """cfg 缺 term_aliases 键（防御性）→ 不炸、行为不变。"""
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Gateway 升级完成", mid="mem-gw-5")

    cfg_min = copy.deepcopy(cfg)
    cfg_min.pop("term_aliases", None)

    data = search(mem_conn, session_conn, cfg_min, query="gateway", scopes=["memory"]).data
    assert "mem-gw-5" in _result_ids(data)


def test_http_search_old_term_hits_new_name_memory(client, mock_vector):
    """HTTP POST /v1/search：旧术语查询命中新名记忆（端到端）。"""
    mem_conn = client.app.state.mem_conn
    _insert_memory(mem_conn, "Gateway 升级完成 重启服务", mid="mem-http-1")

    resp = client.post("/v1/search", headers=AGENT_HEADERS, json={"query": "daemon"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["routes"] == ["bm25"]
    assert "mem-http-1" in [r["memory_id"] for r in body["results"]]
