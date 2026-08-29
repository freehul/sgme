"""T13 测试：向量检索 + RRF 融合（v0.4）。

覆盖：
- embed: 成功 / 不可达
- upsert_memory_vector: 成功落 BLOB / embed 失败不抛
- vector_search: numpy 余弦降级路径
- rrf_merge: 基础 / 同 memory 双路命中 / 空列表
- search_memories: 向量可用 RRF 融合 / 向量不可达降级 BM25
- 序列化/反序列化往返

mock LLM embeddings 用 httpx.MockTransport 注入固定向量。
"""
from __future__ import annotations

import sqlite3
import struct
import uuid

import httpx
import numpy as np
import pytest

from sgme import config
from sgme.data.search import init_fts
from sgme.data.search import rrf as rrf_mod
from sgme.data.search import recall_routes
from sgme.data.search import search_memories as do_search
from sgme.data.search import vector as vector_mod
from sgme.data import db as db_mod
from sgme.data import memory_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    cfg = config.load_config()
    # 测试形态：显式 vector base_url（B123）——mock 客户端按传入 client 发请求，
    # 走主路而非链首回退；T-117 后链首 agnes(vector_capable=false) 回退被门禁拦截
    cfg.setdefault("search", {}).setdefault("vector", {})[
        "base_url"
    ] = "http://mock-embed.test/v1"
    return cfg


@pytest.fixture
def mem_conn(tmp_path, cfg):
    """memory.db + registry 已导入 + FTS5 已初始化。"""
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    init_fts(conn)
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


# ---------- mock helpers ----------

def _mock_embed_client(embedding: list[float]) -> httpx.Client:
    """构造 mock httpx 客户端，对 /embeddings 请求返回固定向量。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"embedding": list(embedding)}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_embed_unavailable_client() -> httpx.Client:
    """构造 mock httpx 客户端，所有请求抛 ConnectError（模拟 LM Studio 不可达）。"""
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _insert_memory(mem_conn, content="测试内容", dim_ids=("tech_stack",)) -> str:
    """插入一条记忆并返回 memory_id。"""
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=list(dim_ids),
    )


# ---------- embed ----------

def test_embed_success(cfg):
    """mock httpx 返回向量数组 → embed 返回该向量。"""
    # Arrange
    expected = [0.1, 0.2, 0.3, 0.4]
    cli = _mock_embed_client(expected)

    # Act
    vec = vector_mod.embed("hello world", cfg, client=cli)

    # Assert
    assert vec is not None
    assert len(vec) == 4
    assert vec == pytest.approx(expected, rel=1e-6)


def test_embed_unavailable(cfg):
    """embeddings 端点不可达 → 返回 None（不抛异常）。"""
    # Arrange
    cli = _mock_embed_unavailable_client()

    # Act
    vec = vector_mod.embed("hello", cfg, client=cli)

    # Assert
    assert vec is None


# ---------- upsert_memory_vector ----------

def test_upsert_memory_vector(mem_conn, cfg):
    """存储后 memory_vectors 表出现 BLOB。"""
    # Arrange
    mid = _insert_memory(mem_conn, content="Python FastAPI")
    embedding = [0.5, 0.5, 0.0, 0.0]
    cli = _mock_embed_client(embedding)

    # Act
    ok = vector_mod.upsert_memory_vector(mem_conn, mid, "Python FastAPI", cfg, cli)

    # Assert
    assert ok is True
    row = memory_dao.get_vector(mem_conn, mid)
    assert row is not None
    assert row["memory_id"] == mid
    assert isinstance(row["embedding"], bytes)
    assert len(row["embedding"]) == 16  # 4 floats × 4 bytes
    assert row["dims"] == 4
    assert row["model"] == cfg["search"]["vector"]["model"]


def test_upsert_memory_vector_embed_failed(mem_conn, cfg):
    """embed 失败 → 返回 False + 不抛异常 + memory_vectors 表无记录。"""
    # Arrange
    mid = _insert_memory(mem_conn, content="测试")
    cli = _mock_embed_unavailable_client()

    # Act
    ok = vector_mod.upsert_memory_vector(mem_conn, mid, "测试", cfg, cli)

    # Assert
    assert ok is False
    assert memory_dao.get_vector(mem_conn, mid) is None


# ---------- vector_search ----------

def test_vector_search_numpy_fallback(mem_conn, cfg, monkeypatch):
    """numpy 余弦降级路径（强制 sqlite-vec 不可用）。"""
    # Arrange：强制 numpy 路径
    monkeypatch.setattr(vector_mod, "_VEC_EXTENSION_LOADED", False)
    monkeypatch.setattr(vector_mod, "_VEC_TRIED_INIT", True)
    monkeypatch.setattr(vector_mod, "_VEC_LOADABLE_PATH", None)

    mid1 = _insert_memory(mem_conn, content="向量一")
    mid2 = _insert_memory(mem_conn, content="向量二")

    # 存储 4 维向量：m1 与 query 方向接近，m2 与 query 方向远离
    memory_dao.upsert_vector(mem_conn, mid1, struct.pack("4f", 1.0, 0.0, 0.0, 0.0), "test", 4)
    memory_dao.upsert_vector(mem_conn, mid2, struct.pack("4f", 0.0, 1.0, 0.0, 0.0), "test", 4)
    query_vec = [0.9, 0.1, 0.0, 0.0]

    # Act
    results = vector_mod.vector_search(mem_conn, query_vec, limit=10)

    # Assert：m1 应排第一（余弦相似更高）
    assert len(results) == 2
    assert results[0]["memory_id"] == mid1
    assert results[1]["memory_id"] == mid2
    # score 单调递减
    assert results[0]["score"] > results[1]["score"]
    # m1 与 query 余弦相似 ≈ 0.9939
    assert results[0]["score"] == pytest.approx(0.99, abs=0.01)


def test_vector_search_numpy_excludes_rejected(mem_conn, cfg, monkeypatch):
    """numpy 降级路径：rejected 记忆不得出现在向量检索结果（2026-08-11 修复）。"""
    # Arrange：强制 numpy 路径
    monkeypatch.setattr(vector_mod, "_VEC_EXTENSION_LOADED", False)
    monkeypatch.setattr(vector_mod, "_VEC_TRIED_INIT", True)
    monkeypatch.setattr(vector_mod, "_VEC_LOADABLE_PATH", None)

    mid1 = _insert_memory(mem_conn, content="向量一")
    mid2 = _insert_memory(mem_conn, content="向量二")
    memory_dao.upsert_vector(mem_conn, mid1, struct.pack("4f", 1.0, 0.0, 0.0, 0.0), "test", 4)
    memory_dao.upsert_vector(mem_conn, mid2, struct.pack("4f", 0.0, 1.0, 0.0, 0.0), "test", 4)
    # reject mid1（与 query 最相似的记忆）
    memory_dao.reject_memory(mem_conn, mid1, "测试：标记不采用")
    query_vec = [0.9, 0.1, 0.0, 0.0]

    # Act
    results = vector_mod.vector_search(mem_conn, query_vec, limit=10)

    # Assert：rejected 的 mid1 必须被排除，只剩 mid2
    ids = [r["memory_id"] for r in results]
    assert mid1 not in ids
    assert mid2 in ids


def test_vector_search_sqlite_vec_sql_filters_rejected():
    """sqlite-vec 路径 SQL 必须带 status != 'rejected' 过滤（防回归）。"""
    sql = vector_mod._sqlite_vec_search.__doc__ or ""
    import inspect
    src = inspect.getsource(vector_mod._sqlite_vec_search)
    assert "status != 'rejected'" in src


# ---------- rrf_merge ----------

def test_rrf_merge_basic():
    """BM25 rank=0 + vector rank=0 同一 memory → RRF score = 2/(60+0+1) = 2/61。"""
    # Arrange
    bm25 = [{"memory_id": "m1", "content": "a", "priority": 50, "updated_at": "t"}]
    vector = [{"memory_id": "m1", "content": "a", "priority": 50, "updated_at": "t"}]

    # Act
    merged = rrf_mod.rrf_merge(bm25, vector, k=60)

    # Assert
    assert len(merged) == 1
    assert merged[0]["memory_id"] == "m1"
    # score = 1/(60+0+1) + 1/(60+0+1) = 2/61
    assert merged[0]["score"] == pytest.approx(2.0 / 61.0, rel=1e-9)
    assert set(merged[0]["sources"]) == {"bm25", "vector"}


def test_rrf_merge_same_memory_both_lists():
    """同一 memory_id 两路命中（不同 rank）→ score 累加。"""
    # Arrange：m1 在 BM25 排第 0，在 vector 排第 1
    bm25 = [
        {"memory_id": "m1", "content": "a", "priority": 50, "updated_at": "t"},
        {"memory_id": "m2", "content": "b", "priority": 50, "updated_at": "t"},
    ]
    vector = [
        {"memory_id": "m2", "content": "b", "priority": 50, "updated_at": "t"},
        {"memory_id": "m1", "content": "a", "priority": 50, "updated_at": "t"},
    ]

    # Act
    merged = rrf_mod.rrf_merge(bm25, vector, k=60)

    # Assert
    by_id = {r["memory_id"]: r for r in merged}
    # m1: BM25 rank=0 → 1/61；vector rank=1 → 1/62；累加
    expected_m1 = 1.0 / 61.0 + 1.0 / 62.0
    assert by_id["m1"]["score"] == pytest.approx(expected_m1, rel=1e-9)
    assert set(by_id["m1"]["sources"]) == {"bm25", "vector"}
    # m2: BM25 rank=1 → 1/62；vector rank=0 → 1/61；累加
    expected_m2 = 1.0 / 62.0 + 1.0 / 61.0
    assert by_id["m2"]["score"] == pytest.approx(expected_m2, rel=1e-9)
    assert set(by_id["m2"]["sources"]) == {"bm25", "vector"}


def test_rrf_merge_empty_lists():
    """空列表 → 空结果。"""
    # Act
    merged = rrf_mod.rrf_merge([], [], k=60)

    # Assert
    assert merged == []


# ---------- search_memories 集成 ----------

def test_search_memories_with_rrf(mem_conn, session_conn, cfg):
    """search_memories 向量可用时返回 RRF 融合结果。"""
    # Arrange：插入一条记忆，并存储 embedding
    mid = _insert_memory(mem_conn, content="Python FastAPI 底座")
    embedding = [1.0, 0.0, 0.0, 0.0]
    cli = _mock_embed_client(embedding)
    # 为已落库记忆补 embedding
    assert vector_mod.upsert_memory_vector(
        mem_conn, mid, "Python FastAPI 底座", cfg, cli
    ) is True

    # Act：搜索 "Python"（BM25 应命中 + 向量应命中）
    results = do_search(
        mem_conn, session_conn,
        query="Python",
        dimensions=["tech_stack"],
        limit=10,
        cfg=cfg,
        client=cli,
    )

    # Assert：RRF 融合后该记忆应同时命中 bm25 + vector
    assert len(results) >= 1
    target = next((r for r in results if r["memory_id"] == mid), None)
    assert target is not None, "目标记忆未命中"
    assert "bm25" in target["sources"]
    assert "vector" in target["sources"]
    assert "rrf" in target["routes"]


def test_search_memories_vector_unavailable(mem_conn, session_conn, cfg):
    """向量不可达时降级纯 BM25（仍返回结果，但 routes 不含 vector）。"""
    # Arrange：插入一条记忆
    mid = _insert_memory(mem_conn, content="Python FastAPI 底座")
    cli = _mock_embed_unavailable_client()

    # Act
    results = do_search(
        mem_conn, session_conn,
        query="Python",
        dimensions=["tech_stack"],
        limit=10,
        cfg=cfg,
        client=cli,
    )

    # Assert：BM25 仍命中，但 routes 不含 vector/rrf
    assert len(results) >= 1
    target = next((r for r in results if r["memory_id"] == mid), None)
    assert target is not None
    assert "vector" not in target["routes"]
    assert "rrf" not in target["routes"]


# ---------- recall_routes（T13 抽出，#32 接入评测套件） ----------

def test_recall_routes_consistency_with_search_memories(mem_conn, session_conn, cfg):
    """recall_routes 抽出自 search_memories 后，同 query 的命中集合与融合结果一致。

    - recall_routes 返回 (bm25, vec, routes)，不做 RRF 融合
    - search_memories 在 vec 非空时执行 rrf_merge（仅重排，不改命中集合）
    - 两者 memory_id 集合应相同（融合不增不减命中，只重排）
    """
    mid = _insert_memory(mem_conn, content="Python FastAPI 底座")
    cli = _mock_embed_client([1.0, 0.0, 0.0, 0.0])
    assert vector_mod.upsert_memory_vector(
        mem_conn, mid, "Python FastAPI 底座", cfg, cli
    ) is True

    bm25, vec, routes = recall_routes(
        mem_conn, "Python", dimensions=["tech_stack"], limit=10, cfg=cfg, client=cli,
    )
    # 向量可用 ⇒ routes 提示调用方执行 RRF 融合
    assert "rrf" in routes
    assert "vector" in routes

    fused = do_search(
        mem_conn, session_conn, query="Python",
        dimensions=["tech_stack"], limit=10, cfg=cfg, client=cli,
    )
    fused_ids = {r["memory_id"] for r in fused}
    recall_ids = {r["memory_id"] for r in bm25} | {r["memory_id"] for r in vec}

    # 融合不增不减命中集合
    assert recall_ids == fused_ids
    assert mid in fused_ids


def test_recall_routes_no_fusion(mem_conn, cfg):
    """recall_routes 不做 RRF 融合：两路结果各自独立，向量可用时 routes 含 rrf。"""
    mid = _insert_memory(mem_conn, content="Python FastAPI 底座")
    cli = _mock_embed_client([1.0, 0.0, 0.0, 0.0])
    assert vector_mod.upsert_memory_vector(
        mem_conn, mid, "Python FastAPI 底座", cfg, cli
    ) is True

    bm25, vec, routes = recall_routes(
        mem_conn, "Python", dimensions=["tech_stack"], limit=10, cfg=cfg, client=cli,
    )
    # bm25 结果保持原始 BM25 列表（未被融合改写）
    assert isinstance(bm25, list)
    assert "rrf" in routes  # 向量可用 ⇒ 提示融合
    # 向量结果独立存在且含目标记忆
    assert any(r["memory_id"] == mid for r in vec)


def test_recall_routes_empty_query(mem_conn, cfg):
    """空 query（仅空白）→ 返回 (空, 空, 空)。"""
    bm25, vec, routes = recall_routes(mem_conn, "   ", cfg=cfg, client=None)
    assert bm25 == [] and vec == [] and routes == []


# ---------- 序列化 ----------

def test_serialize_deserialize_vector():
    """序列化/反序列化往返（float32 精度内一致）。"""
    # Arrange：用 float32 可精确表示的值
    original = [1.0, 2.5, -3.0, 0.0, 100.0]

    # Act
    blob = vector_mod._serialize_vector(original)
    restored = vector_mod._deserialize_vector(blob)

    # Assert
    assert len(blob) == 5 * 4  # 5 floats × 4 bytes
    assert len(restored) == 5
    assert restored == pytest.approx(original, rel=1e-6)


# ---------- 场景层检索（Task 5；v0.7 起 scenes 归 memory.db） ----------

def test_search_scene_scopes(mem_conn, cfg):
    """scopes=wiki 检索场景叙事文档（v0.7 起 scenes 位于 memory.db）。"""
    from sgme.data.search import search_scenes

    # 造场景数据
    mem_conn.execute(
        "INSERT INTO scenes (scene_id, title, content, heat, status, created_at, updated_at) "
        "VALUES ('s1', '家庭安排', '每周三上午的家庭安排', 2, 'active', "
        "'2026-08-04T00:00:00Z', '2026-08-04T00:00:00Z')"
    )
    mem_conn.execute(
        "INSERT INTO scenes (scene_id, title, content, heat, status, created_at, updated_at) "
        "VALUES ('s2', 'SGME 项目', '正在开发记忆引擎，计划月底发布 v1.0', 1, 'active', "
        "'2026-08-04T00:00:00Z', '2026-08-04T00:00:00Z')"
    )
    mem_conn.execute(
        "INSERT INTO scenes (scene_id, title, content, heat, status, created_at, updated_at) "
        "VALUES ('s3', '归档场景', '已归档的旧内容', 3, 'archived', "
        "'2026-08-04T00:00:00Z', '2026-08-04T00:00:00Z')"
    )
    mem_conn.commit()

    # 搜"安排"→ 命中 s1（active 场景）
    results = search_scenes(mem_conn, "安排", limit=10)
    assert len(results) >= 1
    assert results[0]["scene_id"] == "s1"
    assert results[0]["source"] == "wiki_scene"
    assert results[0]["heat"] == 2

    # 搜"记忆引擎"→ 命中 s2
    results2 = search_scenes(mem_conn, "记忆引擎", limit=10)
    assert any(r["scene_id"] == "s2" for r in results2)

    # archived 场景不返回（软删除语义）
    results3 = search_scenes(mem_conn, "归档", limit=10)
    assert all(r["scene_id"] != "s3" for r in results3)


def test_search_scenes_empty_query(mem_conn):
    """空查询 → 空结果。"""
    from sgme.data.search import search_scenes
    assert search_scenes(mem_conn, "  ", limit=10) == []

# ---------- T-89 内容去重 + limit 截断（2026-08-20） ----------

def test_search_memories_dedup_same_content(mem_conn, session_conn, cfg):
    """同一内容被重复落库（不同 memory_id）→ 检索只返回 1 条，不稀释注入。

    实测（2026-08-20）：注入显示 10 条相关记忆 4 对重复——L1 提炼把同一事实
    落库多条，search 全量召回。修复：RRF 融合后按 content 去重。
    """
    # Arrange：两条 content 完全相同的记忆（不同 memory_id）
    mid1 = _insert_memory(mem_conn, content="Python FastAPI 底座")
    mid2 = _insert_memory(mem_conn, content="Python FastAPI 底座")
    cli = _mock_embed_client([1.0, 0.0, 0.0, 0.0])
    for mid in (mid1, mid2):
        assert vector_mod.upsert_memory_vector(
            mem_conn, mid, "Python FastAPI 底座", cfg, cli
        ) is True

    # Act
    results = do_search(
        mem_conn, session_conn,
        query="Python", dimensions=["tech_stack"], limit=10,
        cfg=cfg, client=cli,
    )

    # Assert：相同 content 只出现 1 条
    contents = [r["content"] for r in results]
    assert contents.count("Python FastAPI 底座") == 1
    # 且保留的是 rank 更优的一条（content 去重后仍带完整装饰）
    assert all("memory_id" in r and "rank" in r for r in results)


def test_search_memories_limit_respected(mem_conn, session_conn, cfg):
    """两路召回各满 limit 时，RRF 融合后结果 ≤ limit（不超发）。

    根因：recall_routes 两路各取 limit 条，rrf_merge 按 id 合并不去重不截断
    → 最多返回 2×limit 条。注入 searchLimit=5 却返回 10 条即此 bug。
    """
    # Arrange：插入 12 条记忆（BM25 与向量两路都会命中，各满 limit）
    contents = [f"Python FastAPI 特性{i}" for i in range(12)]
    mids = [_insert_memory(mem_conn, content=c) for c in contents]
    cli = _mock_embed_client([1.0, 0.0, 0.0, 0.0])
    for mid, c in zip(mids, contents):
        assert vector_mod.upsert_memory_vector(mem_conn, mid, c, cfg, cli) is True

    # Act：limit=5
    results = do_search(
        mem_conn, session_conn,
        query="Python", dimensions=["tech_stack"], limit=5,
        cfg=cfg, client=cli,
    )

    # Assert：不超 limit
    assert len(results) <= 5

