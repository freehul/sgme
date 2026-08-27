"""tests/test_operations_search.py：operations 层 search 操作测试（v0.7 P2-T3）。

覆盖：
1. operations.search() 返回 OperationResult(ok=True)，data 为信息超集
   （results / routes / rrf_k）
2. 正常检索：memory 层命中，结果结构完整（rank/source/memory_id/content/
   dimensions/priority/updated_at/trace/routes）
3. 空结果：query 无命中 / 空 query → ok=True，results 空数组（v0.6 行为等价）
4. 参数错误：query 非字符串 / scopes 非字符串列表 → InvalidArgs（ERR_INVALID_ARGS）
5. 底层异常：检索函数抛异常 → OperationResult.fail(ERR_INTERNAL)
6. wiki scope 行为：scopes=["wiki"] / ["scenes"]（历史别名）命中 L2 场景层
7. HTTP/MCP 投影形态：http_payload 含 meta（routes/rrf_k，空命中回退 ["bm25"]）；
   mcp_payload 无 meta
8. **契约等价性**（最关键）：POST /v1/search 与 MCP search 工具经 operations
   包装后，输出与 v0.6 逐字段一致（同一状态下两端共有字段取值相同）
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import search as search_mod
from sgme.data import wiki_dao
from sgme.engine import health as engine_health
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations.errors import ERR_INVALID_ARGS, ERR_INTERNAL, InvalidArgs, OperationResult
from sgme.operations.search import META_RRF_K, http_payload, mcp_payload, search
from sgme.raw import store as raw_store
from sgme.data.search import init_fts, init_scenes_fts
from sgme.data.search import vector as vector_mod
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao, scene_dao, session_dao
from sgme.wiki import fts as wiki_fts_mod
from sgme.wiki.fts import init_wiki_fts

# ---------- v0.6 冻结契约（改造前逐字段抄录，任何变动即破坏性变更） ----------

HTTP_TOP_KEYS = ["results", "meta"]
HTTP_META_KEYS = ["routes", "rrf_k"]
MCP_TOP_KEYS = ["results"]
MEMORY_RESULT_KEYS = [
    "rank", "score", "source", "memory_id", "content", "dimensions",
    "priority", "updated_at", "trace", "routes",
]
SCENE_RESULT_KEYS = [
    "rank", "source", "scene_id", "title", "content", "heat",
    "updated_at", "routes",
]
WIKI_PAGES_RESULT_KEYS = [
    "rank", "source", "page_id", "title", "content", "category", "tags", "routes",
]

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


# ---------- fixtures（照抄 test_operations_health.py，扩展 FTS/向量隔离） ----------

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
    """mock LLM 探测为可用（避免实际打 127.0.0.1:1014）。"""
    monkeypatch.setattr(
        engine_health, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "lm-studio",
            "model": "mock-model", "error": None,
        },
    )


@pytest.fixture
def mock_vector(monkeypatch):
    """mock 向量 embed 为不可用（返回 None → 检索自动降级纯 BM25/LIKE）。

    与 test_server_v04 的降级测试同一口径：避免测试依赖真实 embeddings 端点，
    保证离线 CI 确定性。monkeypatch 的是 sgme.search.vector 模块全局 embed，
    检索内部按模块全局解析，operations 层与入口层调用同样生效。
    """
    monkeypatch.setattr(vector_mod, "embed", lambda query, cfg, client=None: None)


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）+ FTS 虚拟表初始化（搜索必需）。

    照抄 health 的 conns 基础上追加 ``init_fts`` / ``init_scenes_fts``：
    health 不查 FTS 所以不需要；搜索要走 BM25 主路必须建 memories_fts /
    scenes_fts（create_app 内部同样调用这两个函数）。
    """
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
def app(conns, cfg, raw_dir, mock_llm, tmp_path, monkeypatch):
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


@pytest.fixture
def mcp(conns, cfg, raw_dir, mock_llm):
    """绑定同一批连接的 MCP server。"""
    mem_conn, session_conn, wiki_conn = conns
    bind_app_state({
        "cfg": cfg, "mem_conn": mem_conn,
        "session_conn": session_conn, "wiki_conn": wiki_conn,
    })
    return build_mcp_server()


# ---------- 工具 ----------

def _insert_memory(mem_conn: sqlite3.Connection, content: str, mid: str | None = None) -> str:
    """插入一条测试记忆（tech_stack 维度 + 可选溯源）。"""
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
        memory_id=mid,
        sources=None,
    )


def _insert_scene(mem_conn: sqlite3.Connection, scene_id: str,
                  title: str, content: str) -> str:
    """插入一条 L2 场景（heat=1, status='active'）。"""
    return scene_dao.insert_scene(mem_conn, scene_id=scene_id, title=title, content=content)


def _insert_wiki_page(wiki_conn: sqlite3.Connection, page_id: str,
                      title: str, content: str) -> str:
    """插入一条 wiki 知识库页面（T-34 测试数据，幂等 upsert）。"""
    return wiki_dao.insert_page(wiki_conn, page_id=page_id, title=title, content=content)


def _call_mcp(mcp_server, name: str, args: dict) -> str:
    """同步包装 async call_tool → 返回文本。"""
    raw = asyncio.run(mcp_server.call_tool(name, args))
    results, _meta = raw if isinstance(raw, tuple) else (raw, None)
    return "\n".join(c.text for c in results if getattr(c, "text", None))


# ---------- 1. 正常检索成功 ----------

def test_search_returns_operation_result_ok(conns, cfg, mock_vector):
    """operations.search() 返回 OperationResult(ok=True)，data 为信息超集。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    res = search(mem_conn, session_conn, cfg, query="Python", scopes=["memory"])

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert set(res.data.keys()) == {"results", "routes", "rrf_k"}
    assert res.data["rrf_k"] == META_RRF_K == 60


def test_search_memory_result_shape_complete(conns, cfg, mock_vector):
    """memory 层命中的结果结构完整（v0.6 逐字段等价）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    mid = _insert_memory(mem_conn, "Python FastAPI 底座设计", mid="mem-search-1")

    # Act
    data = search(mem_conn, session_conn, cfg, query="Python", scopes=["memory"]).data

    # Assert
    assert len(data["results"]) >= 1
    first = data["results"][0]
    # 结果字典键序由 SQL 行 + 装饰顺序决定（非响应契约），用集合比较
    assert set(first.keys()) == set(MEMORY_RESULT_KEYS)
    assert first["source"] == "memory"
    assert first["memory_id"] == mid
    assert first["rank"] == 1
    assert first["routes"] == ["bm25"]
    assert data["routes"] == ["bm25"]
    # include_sources=True 时 trace 键必须存在（可为空列表）
    assert isinstance(first["trace"], list)


def test_search_memory_with_dimensions_adds_label_route(conns, cfg, mock_vector):
    """带 dimensions 检索 → routes 含 label（v0.6 行为）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    data = search(
        mem_conn, session_conn, cfg,
        query="Python", scopes=["memory"], dimensions=["tech_stack"], match="any",
    ).data

    # Assert
    assert len(data["results"]) >= 1
    assert data["results"][0]["routes"] == ["bm25", "label"]
    assert data["routes"] == ["bm25", "label"]


# ---------- 2. 空结果 ----------

def test_search_no_match_returns_empty_results(conns, cfg, mock_vector):
    """query 无命中 → ok=True，results 空数组（v0.6 行为等价）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    res = search(mem_conn, session_conn, cfg, query="zzzzqqqq不存在的词", scopes=["memory"])

    # Assert
    assert res.ok is True
    assert res.data["results"] == []
    assert res.data["routes"] == []


def test_search_empty_query_returns_empty_results(conns, cfg, mock_vector):
    """空 query → ok=True，results 空数组（v0.6 不报错，tests/test_server.py 同用例）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    res = search(mem_conn, session_conn, cfg, query="", scopes=["memory"])

    # Assert
    assert res.ok is True
    assert res.data["results"] == []


# ---------- 3. 参数错误 → ERR_INVALID_ARGS ----------

@pytest.mark.parametrize("bad_query", [None, 123, ["Python"]])
def test_search_invalid_query_raises_invalid_args(conns, cfg, mock_vector, bad_query):
    """query 非字符串 → InvalidArgs（ERR_INVALID_ARGS，镜像 pydantic 校验）。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    # Act / Assert
    with pytest.raises(InvalidArgs) as ei:
        search(mem_conn, session_conn, cfg, query=bad_query)
    assert ei.value.error_code == ERR_INVALID_ARGS


@pytest.mark.parametrize("bad_scopes", ["memory", ["memory", 1], {"memory"}])
def test_search_invalid_scopes_raises_invalid_args(conns, cfg, mock_vector, bad_scopes):
    """scopes 非字符串列表 → InvalidArgs（ERR_INVALID_ARGS）。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    # Act / Assert
    with pytest.raises(InvalidArgs) as ei:
        search(mem_conn, session_conn, cfg, query="Python", scopes=bad_scopes)
    assert ei.value.error_code == ERR_INVALID_ARGS


def test_search_scopes_none_defaults_to_memory(conns, cfg, mock_vector):
    """scopes=None → 缺省 ["memory","skills"]（v0.6 SearchRequest pydantic 缺省语义扩展）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    res = search(mem_conn, session_conn, cfg, query="Python")

    # Assert
    assert res.ok is True
    assert len(res.data["results"]) >= 1
    # 新缺省含 skills，但本测试 cfg 未配置 skills 源 → 该层隔离为空
    assert all(r["source"] != "skills" for r in res.data["results"])


def test_search_unknown_scope_ignored(conns, cfg, mock_vector):
    """未知 scope 值被忽略 → 空结果（v0.6 行为，不报错）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    res = search(mem_conn, session_conn, cfg, query="Python", scopes=["wiki_raw"])

    # Assert
    assert res.ok is True
    assert res.data["results"] == []


# ---------- 4. 底层异常 → ERR_INTERNAL ----------

def test_search_internal_error_maps_err_internal(conns, cfg, mock_vector, monkeypatch):
    """检索函数抛异常 → OperationResult.fail(ERR_INTERNAL)，不向上炸。"""
    # Arrange：monkeypatch 业务层检索函数抛异常
    mem_conn, session_conn, _ = conns

    def _boom(*args, **kwargs):
        raise RuntimeError("检索后端故障")

    monkeypatch.setattr(search_mod, "search_memories", _boom)

    # Act
    res = search(mem_conn, session_conn, cfg, query="Python", scopes=["memory"])

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert res.data is None
    assert "检索失败" in res.message


# ---------- 5. wiki scope 行为 ----------

def test_search_wiki_scope_returns_scenes(conns, cfg, mock_vector):
    """scopes=["wiki"] → 命中 L2 场景层（source=wiki_scene）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_scene(mem_conn, "scene-1", "量子计算底座", "Python FastAPI 架构设计 量子计算")

    # Act
    res = search(mem_conn, session_conn, cfg, query="量子计算", scopes=["wiki"])

    # Assert
    assert res.ok is True
    assert len(res.data["results"]) >= 1
    first = res.data["results"][0]
    assert first["source"] == "wiki_scene"
    assert first["scene_id"] == "scene-1"
    assert "wiki_bm25" in first["routes"] or "wiki_like" in first["routes"]
    assert "wiki_bm25" in res.data["routes"] or "wiki_like" in res.data["routes"]


def test_search_scenes_alias_scope_equivalent_to_wiki(conns, cfg, mock_vector):
    """scopes=["scenes"] 是 "wiki" 的历史别名，行为等价。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_scene(mem_conn, "scene-alias", "量子计算底座", "量子计算 内容")

    # Act
    res = search(mem_conn, session_conn, cfg, query="量子计算", scopes=["scenes"])

    # Assert
    assert res.ok is True
    assert any(r["scene_id"] == "scene-alias" for r in res.data["results"])


def test_search_multi_scope_merges_memory_and_wiki(conns, cfg, mock_vector):
    """memory + wiki 双 scope → 结果先 memory 后 wiki，routes 取并集。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "量子计算底座 Python 记忆")
    _insert_scene(mem_conn, "scene-multi", "量子计算底座", "量子计算 场景文档")

    # Act
    res = search(mem_conn, session_conn, cfg, query="量子计算", scopes=["memory", "wiki"])

    # Assert
    assert res.ok is True
    sources = [r["source"] for r in res.data["results"]]
    assert "memory" in sources
    assert "wiki_scene" in sources
    # 顺序：memory 层在前
    assert sources.index("memory") < sources.index("wiki_scene")
    # routes 并集：bm25（memory）+ wiki 路
    assert "bm25" in res.data["routes"]
    assert any(rt.startswith("wiki") for rt in res.data["routes"])


# ---------- 5b. wiki_pages scope 行为（T-34） ----------

def test_search_wiki_pages_scope_returns_pages(conns, cfg, mock_vector):
    """scopes=["wiki_pages"] + wiki_conn → 命中 wiki 知识库页面（source=wiki_pages）。"""
    # Arrange
    mem_conn, session_conn, wiki_conn = conns
    _insert_wiki_page(wiki_conn, "page-1", "SGME 架构", "SGME 记忆引擎 架构设计 知识库")

    # Act
    res = search(mem_conn, session_conn, cfg,
                 query="SGME", scopes=["wiki_pages"], wiki_conn=wiki_conn)

    # Assert
    assert res.ok is True
    assert len(res.data["results"]) >= 1
    first = res.data["results"][0]
    assert set(first.keys()) == set(WIKI_PAGES_RESULT_KEYS)
    assert first["source"] == "wiki_pages"
    assert first["page_id"] == "page-1"
    assert first["title"] == "SGME 架构"
    assert "wiki_fts" in first["routes"]
    assert "wiki_fts" in res.data["routes"]


def test_search_wiki_pages_conn_none_skips_layer(conns, cfg, mock_vector):
    """wiki_conn=None（wiki 扩展未挂载）→ 该层空结果，不影响 memory 层。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    res = search(mem_conn, session_conn, cfg,
                 query="Python", scopes=["memory", "wiki_pages"], wiki_conn=None)

    # Assert
    assert res.ok is True
    sources = [r["source"] for r in res.data["results"]]
    assert "memory" in sources
    assert "wiki_pages" not in sources


def test_search_wiki_pages_failure_isolated(conns, cfg, mock_vector, monkeypatch):
    """wiki_pages 层检索失败 → 空结果 + memory 层正常（容错隔离，不整体 500）。"""
    # Arrange
    mem_conn, session_conn, wiki_conn = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated wiki fts failure")

    monkeypatch.setattr(wiki_fts_mod, "search_wiki_fts", _boom)

    # Act
    res = search(mem_conn, session_conn, cfg,
                 query="Python", scopes=["memory", "wiki_pages"], wiki_conn=wiki_conn)

    # Assert
    assert res.ok is True
    sources = [r["source"] for r in res.data["results"]]
    assert "memory" in sources
    assert "wiki_pages" not in sources


def test_search_multi_scope_includes_wiki_pages(conns, cfg, mock_vector):
    """memory + wiki + wiki_pages 三层组合 → 按 scope 顺序拼接，routes 并集。"""
    # Arrange
    mem_conn, session_conn, wiki_conn = conns
    _insert_memory(mem_conn, "量子计算底座 Python 记忆")
    _insert_scene(mem_conn, "scene-multi2", "量子计算底座", "量子计算 场景文档")
    _insert_wiki_page(wiki_conn, "page-multi", "量子计算", "量子计算 知识库页面")

    # Act
    res = search(mem_conn, session_conn, cfg,
                 query="量子计算", scopes=["memory", "wiki", "wiki_pages"],
                 wiki_conn=wiki_conn)

    # Assert
    assert res.ok is True
    sources = [r["source"] for r in res.data["results"]]
    assert "memory" in sources
    assert "wiki_scene" in sources
    assert "wiki_pages" in sources
    assert sources.index("memory") < sources.index("wiki_scene") < sources.index("wiki_pages")


def test_http_endpoint_wiki_pages_scope(client, conns, mock_vector):
    """HTTP /v1/search 经 app.state.wiki_conn 注入 → wiki_pages 层可用。"""
    # Arrange
    mem_conn, session_conn, wiki_conn = conns
    _insert_wiki_page(wiki_conn, "page-http", "SGME 架构", "SGME 记忆引擎 知识库")

    # Act
    resp = client.post("/v1/search",
                       json={"query": "SGME", "scopes": ["wiki_pages"]},
                       headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(r["source"] == "wiki_pages" for r in body["results"])


# ---------- 5c. sessions scope 行为（ST-33：L0 原始层接入 /v1/search） ----------

#: sessions 结果键（与 memory / wiki_pages 同构：rank/source/标识/内容/routes）。
SESSIONS_RESULT_KEYS = [
    "rank", "source", "file_id", "session_key", "agent_id",
    "content", "started_at", "status", "routes",
]


def _insert_raw_file(session_conn: sqlite3.Connection, file_id: str,
                     session_key: str, agent_id: str = "hermes",
                     started_at: str = "2026-08-03T11:18:06Z",
                     status: str = "new") -> None:
    """插入 raw_files 索引行（正文在磁盘，索引只存元数据）。"""
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=f"raw/sessions/{file_id}.md",
        session_key=session_key, agent_id=agent_id,
        started_at=started_at, status=status,
    )


def _write_raw_md(raw_dir, file_id: str, body: str) -> None:
    """写 L0 原文文件（frontmatter + 消息块），供 sessions 层读盘摘要。"""
    d = raw_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{file_id}.md").write_text(
        f"---\nfile_id: {file_id}\nsource_type: session\n---\n\n{body}",
        encoding="utf-8",
    )


def test_search_sessions_scope_returns_raw_files(conns, cfg, mock_vector, raw_dir):
    """scopes=["sessions"] → 命中 raw_files 元数据 + 读盘正文摘要（ST-33）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    cfg["paths"]["raw_dir"] = str(raw_dir)
    _insert_raw_file(session_conn, "sess-1", "hermes-20260803", agent_id="hermes")
    _write_raw_md(raw_dir, "sess-1",
                  "# 2026-08-03T11:18:06Z user\n\nSGME 会话正文 Python FastAPI 底座设计")

    # Act
    res = search(mem_conn, session_conn, cfg, query="hermes", scopes=["sessions"])

    # Assert
    assert res.ok is True
    assert len(res.data["results"]) >= 1
    first = res.data["results"][0]
    assert set(first.keys()) == set(SESSIONS_RESULT_KEYS)
    assert first["source"] == "sessions"
    assert first["file_id"] == "sess-1"
    assert first["session_key"] == "hermes-20260803"
    assert first["agent_id"] == "hermes"
    assert first["routes"] == ["l0_like"]
    # 正文摘要：frontmatter 已剥除，正文关键词可检索到
    assert "SGME 会话正文" in first["content"]
    assert "l0_like" in res.data["routes"]


def test_search_sessions_no_match_empty(conns, cfg, mock_vector, raw_dir):
    """sessions scope 无命中 → ok=True，results 空数组。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    cfg["paths"]["raw_dir"] = str(raw_dir)
    _insert_raw_file(session_conn, "sess-1", "hermes-20260803")

    # Act
    res = search(mem_conn, session_conn, cfg, query="zzzzqqqq不存在的词", scopes=["sessions"])

    # Assert
    assert res.ok is True
    assert res.data["results"] == []
    assert res.data["routes"] == []


def test_search_sessions_merges_with_memory(conns, cfg, mock_vector, raw_dir):
    """memory + sessions 双 scope → 结果先 memory 后 sessions，routes 并集。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    cfg["paths"]["raw_dir"] = str(raw_dir)
    _insert_memory(mem_conn, "量子计算底座 Python 记忆")
    _insert_raw_file(session_conn, "sess-m", "hermes-量子计算", agent_id="hermes")

    # Act
    res = search(mem_conn, session_conn, cfg,
                 query="量子计算", scopes=["memory", "sessions"])

    # Assert
    assert res.ok is True
    sources = [r["source"] for r in res.data["results"]]
    assert "memory" in sources
    assert "sessions" in sources
    # 顺序：memory 层在前（按 scopes 顺序拼接）
    assert sources.index("memory") < sources.index("sessions")
    # routes 并集：bm25（memory）+ l0_like（sessions）
    assert "bm25" in res.data["routes"]
    assert "l0_like" in res.data["routes"]


def test_search_sessions_disk_missing_snippet_empty(conns, cfg, mock_vector, raw_dir):
    """索引命中但磁盘原文缺失 → 元数据仍返回，content 为空串（best-effort）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    cfg["paths"]["raw_dir"] = str(raw_dir)
    _insert_raw_file(session_conn, "sess-missing", "hermes-20260805")

    # Act
    res = search(mem_conn, session_conn, cfg, query="hermes", scopes=["sessions"])

    # Assert
    assert res.ok is True
    assert len(res.data["results"]) == 1
    first = res.data["results"][0]
    assert first["file_id"] == "sess-missing"
    assert first["content"] == ""


def test_http_endpoint_sessions_scope(client, conns, cfg, mock_vector, raw_dir):
    """HTTP /v1/search scopes=["sessions"] → 命中 L0 会话（ST-33 端到端）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    cfg["paths"]["raw_dir"] = str(raw_dir)
    _insert_raw_file(session_conn, "sess-http", "hermes-http-1", agent_id="hermes")
    _write_raw_md(raw_dir, "sess-http",
                  "# 2026-08-03T11:18:06Z user\n\nHTTP 会话正文")

    # Act
    resp = client.post("/v1/search",
                       json={"query": "hermes", "scopes": ["sessions"]},
                       headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(r["source"] == "sessions" for r in body["results"])
    assert body["meta"]["routes"] == ["l0_like"]


# ---------- 6. HTTP / MCP 投影形态 ----------

def test_http_payload_shape(conns, cfg, mock_vector):
    """http_payload：results + meta{routes, rrf_k}，字段顺序与 v0.6 一致。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    data = search(mem_conn, session_conn, cfg, query="Python", scopes=["memory"]).data
    body = http_payload(data)

    # Assert
    assert list(body.keys()) == HTTP_TOP_KEYS
    assert list(body["meta"].keys()) == HTTP_META_KEYS
    assert body["meta"]["rrf_k"] == 60
    assert len(body["results"]) >= 1


def test_http_payload_routes_fallback_bm25(conns, cfg, mock_vector):
    """无命中 → meta.routes 回退 ["bm25"]（v0.6 路由 routes_seen or ["bm25"]）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    data = search(mem_conn, session_conn, cfg, query="zzzzqqqq", scopes=["memory"]).data
    body = http_payload(data)

    # Assert
    assert body["results"] == []
    assert body["meta"]["routes"] == ["bm25"]


def test_mcp_payload_shape(conns, cfg, mock_vector):
    """mcp_payload：仅 {"results": [...]}，无 meta（v0.6 MCP 契约差异）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    data = search(mem_conn, session_conn, cfg, query="Python", scopes=["memory"]).data
    body = mcp_payload(data)

    # Assert
    assert list(body.keys()) == MCP_TOP_KEYS
    assert len(body["results"]) >= 1


# ---------- 7. 契约等价性（最关键） ----------

def test_http_endpoint_contract_unchanged(client, conns, cfg, mock_vector):
    """POST /v1/search 经 operations 包装后，输出与 v0.6 逐字段一致。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act：入口层（现路由）与 operations + http_payload 双路对照
    resp = client.post("/v1/search", json={"query": "Python", "scopes": ["memory"]},
                       headers=AGENT_HEADERS)
    assert resp.status_code == 200, resp.text
    route_body = resp.json()
    op_body = http_payload(
        search(mem_conn, session_conn, cfg,
               query="Python", scopes=["memory"]).data
    )

    # Assert：逐字段等价
    assert list(route_body.keys()) == HTTP_TOP_KEYS
    assert list(op_body.keys()) == HTTP_TOP_KEYS
    assert route_body["meta"] == op_body["meta"] == {"routes": ["bm25"], "rrf_k": 60}
    assert len(route_body["results"]) == len(op_body["results"]) >= 1
    assert route_body["results"][0]["memory_id"] == op_body["results"][0]["memory_id"]
    assert route_body["results"][0]["routes"] == op_body["results"][0]["routes"]


def test_mcp_tool_contract_unchanged(mcp, conns, cfg, mock_vector):
    """MCP search 工具经 operations 包装后，输出与 v0.6 逐字段一致。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act：MCP 工具（现实现）与 operations + mcp_payload 双路对照
    text = _call_mcp(mcp, "search", {"query": "Python"})
    mcp_body = json.loads(text)
    op_body = mcp_payload(
        search(mem_conn, session_conn, cfg,
               query="Python", scopes=["memory"], limit=5).data
    )

    # Assert：MCP 只有 results 键（无 meta），且结果一致
    assert list(mcp_body.keys()) == MCP_TOP_KEYS
    assert list(op_body.keys()) == MCP_TOP_KEYS
    assert len(mcp_body["results"]) == len(op_body["results"]) >= 1
    assert mcp_body["results"][0]["memory_id"] == op_body["results"][0]["memory_id"]


def test_http_and_mcp_agree_on_shared_fields(client, mcp, conns, mock_vector):
    """同一状态下两端输出的共有字段（results）取值一致（差异只在 meta）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    http_body = client.post("/v1/search", json={"query": "Python", "scopes": ["memory"]},
                            headers=AGENT_HEADERS).json()
    mcp_body = json.loads(_call_mcp(mcp, "search", {"query": "Python"}))

    # Assert：HTTP 有 meta、MCP 没有（历史契约差异）；results 同为该记忆
    assert "meta" in http_body
    assert "meta" not in mcp_body
    assert http_body["results"][0]["memory_id"] == mcp_body["results"][0]["memory_id"]
    assert http_body["results"][0]["content"] == mcp_body["results"][0]["content"]


# ---------- 8. skills scope 行为（ST-36 M2：四级披露读侧接入统一搜索） ----------

SKILLS_RESULT_KEYS = ["rank", "name", "score", "source", "description", "category", "routes"]


def _insert_skill_md(tmp_path: Path, name: str, text: str) -> str:
    """造一个 git 源技能目录（<dir>/<name>/SKILL.md），返回目录路径。"""
    d = tmp_path / "skills_tree"
    (d / name).mkdir(parents=True, exist_ok=True)
    (d / name / "SKILL.md").write_text(text, encoding="utf-8")
    return str(d)


def _skills_cfg(source_dir: str) -> dict:
    return {"skills": {"enabled": True, "source_dirs": [source_dir], "budget": 40}}


NAS_SKILL_MD = (
    "---\nname: nas-deploy\ndescription: 飞牛 NAS 部署技能\nversion: 1.0.0\n---\n"
    "# NAS 部署\n docker compose 用法"
)
DOUYIN_SKILL_MD = (
    "---\nname: douyin-pipeline\ndescription: 抖音视频分析入口\nversion: 1.0.0\n---\n"
    "# 抖音采集\n yt-dlp cookies 流水线"
)


def test_search_skills_scope_hits_bm25(conns, cfg, mock_vector, tmp_path):
    """scopes=["skills"] → 命中 git 源技能（source=skills，routes=skills_bm25）。"""
    # Arrange：cfg 注入 skills 段（指向 tmp 技能树）
    mem_conn, session_conn, wiki_conn = conns
    cfg["skills"] = {"enabled": True,
                     "source_dirs": [_insert_skill_md(tmp_path, "nas-deploy", NAS_SKILL_MD)],
                     "budget": 40}

    # Act
    res = search(mem_conn, session_conn, cfg,
                 query="NAS 部署", scopes=["skills"], wiki_conn=wiki_conn)

    # Assert
    assert res.ok is True
    hits = [r for r in res.data["results"] if r["source"] == "skills"]
    assert hits and hits[0]["name"] == "nas-deploy"
    first = hits[0]
    assert set(first.keys()) >= set(SKILLS_RESULT_KEYS)
    assert first["routes"] == ["skills_bm25"]
    assert "skills_bm25" in res.data["routes"]


def test_search_skills_scope_no_cfg_empty_layer(conns, cfg, mock_vector):
    """/v1/search 无 skills 配置段（未配置 source_dirs）→ 该层空结果不报错。"""
    # Arrange
    mem_conn, session_conn, wiki_conn = conns

    # Act
    res = search(mem_conn, session_conn, cfg, query="NAS", scopes=["skills"], wiki_conn=wiki_conn)

    # Assert
    assert res.ok is True
    assert [r for r in res.data["results"] if r["source"] == "skills"] == []


def test_search_skills_scope_failure_isolated(conns, cfg, mock_vector, tmp_path, monkeypatch):
    """skills 层检索失败 → 空结果，memory 层正常（容错隔离镜像 wiki_pages 层）。"""
    # Arrange
    from sgme.operations import skills as ops_skills

    mem_conn, session_conn, wiki_conn = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")
    cfg["skills"] = {"enabled": True,
                     "source_dirs": [_insert_skill_md(tmp_path, "nas-deploy", NAS_SKILL_MD)],
                     "budget": 40}

    def _boom(*args, **kwargs):
        raise RuntimeError("skills 层故障")

    monkeypatch.setattr(ops_skills, "search_skills", _boom)

    # Act
    res = search(mem_conn, session_conn, cfg,
                 query="Python", scopes=["memory", "skills"], wiki_conn=wiki_conn)

    # Assert
    assert res.ok is True
    sources = [r["source"] for r in res.data["results"]]
    assert "memory" in sources
    assert "skills" not in sources


def test_search_skills_scope_absent_does_not_affect_memory(conns, cfg, mock_vector):
    """scopes 不含 skills（既有行为不变）→ memory 层照常，无 skills 结果。"""
    # Arrange
    mem_conn, session_conn, wiki_conn = conns
    _insert_memory(mem_conn, "Python FastAPI 底座设计")

    # Act
    res = search(mem_conn, session_conn, cfg, query="Python", scopes=["memory"], wiki_conn=wiki_conn)

    # Assert
    assert res.ok is True
    sources = [r["source"] for r in res.data["results"]]
    assert "memory" in sources and "skills" not in sources
