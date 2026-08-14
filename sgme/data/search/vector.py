"""search/vector.py：向量检索（sqlite-vec + numpy 降级）。

sqlite-vec 不可用时降级为内存 numpy 余弦相似。
embeddings 端点不可达时返回 None（调用方降级纯 BM25）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import struct
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from sgme.llm import provider as llm_provider
from sgme.data import memory_dao

logger = logging.getLogger("sgme.data.search.vector")

# sqlite-vec 加载状态（首次尝试后缓存）
_VEC_EXTENSION_LOADED = False
_VEC_LOADABLE_PATH: str | None = None
_VEC_TRIED_INIT = False

# 默认 embedding 模型（cfg 缺省兜底）
_DEFAULT_EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"

# 可插拔 embedding 缓存（**生产恒为 None**，仅评测套件安装）。
#
# 为什么钩子必须在 sgme 侧而不能只在 eval 侧包一层：
# 语料落库走的是 `upsert_memory_vector`（eval 可以自己包），
# 但检索时的 query 向量是 `sgme.data.search.recall_routes` 内部直接调 `embed()` 生成的，
# eval 拦不到。少了 query 侧缓存，离线 CI 依然打不出向量通路。
# 因此在最底层的 `embed()` 上开一个可选钩子，是覆盖两侧的唯一最小改动。
_EMBED_CACHE: Any | None = None


def set_embed_cache(cache: Any | None) -> Any | None:
    """安装/卸载 embedding 缓存，返回**旧值**（便于 finally 精确还原）。

    cache 需实现两个方法（duck typing，不引入 sgme → eval 的反向依赖）：
      - ``get(text: str, model: str) -> list[float] | None``
      - ``put(text: str, model: str, vector: list[float]) -> Any``

    传 None 卸载。生产代码路径不调用本函数，行为与改动前完全一致。
    """
    global _EMBED_CACHE
    prev = _EMBED_CACHE
    _EMBED_CACHE = cache
    return prev


def get_embed_cache() -> Any | None:
    """返回当前安装的 embedding 缓存（未安装为 None）。"""
    return _EMBED_CACHE


def try_load_vec_extension(conn: sqlite3.Connection) -> bool:
    """尝试加载 sqlite-vec 扩展（Windows 二进制兼容风险）。

    成功返回 True，失败返回 False（降级 numpy 余弦）。
    首次尝试后缓存 loadable_path；每条连接仍需各自 load_extension。
    """
    global _VEC_EXTENSION_LOADED, _VEC_LOADABLE_PATH, _VEC_TRIED_INIT

    # 首次：尝试发现 sqlite_vec 包路径
    if not _VEC_TRIED_INIT:
        _VEC_TRIED_INIT = True
        try:
            import sqlite_vec  # noqa: F401
            _VEC_LOADABLE_PATH = sqlite_vec.loadable_path()
        except Exception as e:
            logger.warning("sqlite-vec 包不可用，降级 numpy 余弦: %s", e)
            _VEC_LOADABLE_PATH = None
            _VEC_EXTENSION_LOADED = False
            return False

    if _VEC_LOADABLE_PATH is None:
        return False

    # 在该连接上加载扩展
    try:
        conn.enable_load_extension(True)
        conn.load_extension(_VEC_LOADABLE_PATH)
        _VEC_EXTENSION_LOADED = True
        return True
    except Exception as e:
        logger.warning("sqlite-vec 扩展加载失败，降级 numpy 余弦: %s", e)
        _VEC_EXTENSION_LOADED = False
        return False


def embed(
    text: str,
    cfg: dict,
    client: httpx.Client | None = None,
) -> list[float] | None:
    """调 LM Studio embeddings 接口生成向量。

    - cfg: 全局配置（取 search.vector.model 和 LLM 链首批 base_url）
    - embeddings 不可达 → 返回 None + 日志降级
    - httpx 必须 trust_env=False
    """
    search_cfg = cfg.get("search", {}) or {}
    vec_cfg = search_cfg.get("vector", {}) or {}
    model = vec_cfg.get("model", _DEFAULT_EMBED_MODEL)

    # 缓存优先：命中即返回，完全不碰网络（离线 CI 的唯一可行路径）。
    # 放在 base_url 解析之前——命中时连 base_url 都不需要存在。
    cache = _EMBED_CACHE
    if cache is not None:
        try:
            cached = cache.get(text, model)
        except Exception as e:
            logger.warning("embed: 缓存查询异常，降级为真实请求: %s", e)
            cached = None
        if cached:
            return list(cached)

    # base_url 优先级：search.vector.base_url（独立 embedding 端点）→ LLM 链首批（向后兼容）
    base_url = vec_cfg.get("base_url") or ""
    if not base_url:
        try:
            base_url = cfg["llm"]["chains"]["refinement"][0]["base_url"]
        except (KeyError, IndexError, TypeError):
            logger.warning("embed: 无法从 cfg 解析 base_url，跳过向量")
            return None
    base_url = base_url.rstrip("/")
    url = f"{base_url}/embeddings"
    # 鉴权：search.vector.api_key_env 声明 key 环境变量名 → Bearer 头（本地 LM Studio 无需）
    headers = {}
    api_key_env = vec_cfg.get("api_key_env") or ""
    if api_key_env:
        key = os.environ.get(api_key_env, "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        else:
            logger.warning("embed: api_key_env=%s 未设置，请求将不带鉴权头", api_key_env)
    own_client = client is None
    cli = client or llm_provider.make_client(timeout_s=30.0)
    resp: httpx.Response | None = None
    try:
        # 429 限流退避重试（方舟账户级 QPS；最多 3 次，指数退避）
        for attempt in range(4):
            try:
                resp = cli.post(url, json={"model": model, "input": text}, headers=headers)
            except httpx.HTTPError as e:
                logger.warning("embed: embeddings 端点不可达 %s: %s", url, e)
                return None
            if resp.status_code == 429 and attempt < 3:
                wait = 1.5 * (2 ** attempt)
                logger.warning("embed: 429 限流，%.1fs 后重试 (%d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue
            break
        assert resp is not None
        if resp.status_code != 200:
            logger.warning(
                "embed: embeddings 端点返回 %s: %s",
                resp.status_code, resp.text[:200],
            )
            return None
        data = resp.json()
        embedding = data["data"][0]["embedding"]
        vec = [float(x) for x in embedding]
        if cache is not None and vec:
            try:
                cache.put(text, model, vec)
            except Exception as e:
                logger.warning("embed: 缓存写入异常（不影响本次结果）: %s", e)
        return vec
    except Exception as e:
        logger.warning("embed: 解析 embeddings 响应失败: %s", e)
        return None
    finally:
        if own_client:
            cli.close()


def upsert_memory_vector(
    mem_conn: sqlite3.Connection,
    memory_id: str,
    text: str,
    cfg: dict,
    client: httpx.Client | None = None,
) -> bool:
    """生成并存储 embedding（含 model + dims）。

    返回 True 成功，False 失败（embeddings 不可达）。
    """
    vec = embed(text, cfg, client=client)
    if vec is None:
        return False
    search_cfg = cfg.get("search", {}) or {}
    vec_cfg = search_cfg.get("vector", {}) or {}
    model = vec_cfg.get("model", _DEFAULT_EMBED_MODEL)
    blob = _serialize_vector(vec)
    memory_dao.upsert_vector(
        mem_conn, memory_id, blob, model, dims=len(vec),
    )
    return True


def vector_search(
    mem_conn: sqlite3.Connection,
    query_vec: list[float],
    limit: int = 10,
) -> list[dict]:
    """向量检索：sqlite-vec 余弦相似 OR numpy 内存余弦降级。

    返回 [{memory_id, content, priority, updated_at, score}]
    （score=相似度 0-1，越高越相似；按 score DESC 排序）
    """
    # 每条连接都需各自加载扩展（全局 flag 仅表示包是否可用）
    try_load_vec_extension(mem_conn)

    if _VEC_EXTENSION_LOADED:
        try:
            return _sqlite_vec_search(mem_conn, query_vec, limit)
        except Exception as e:
            logger.warning("sqlite-vec 检索失败，降级 numpy: %s", e)

    return _numpy_cosine_search(mem_conn, query_vec, limit)


# ---------- sqlite-vec 路径 ----------

def _sqlite_vec_search(
    mem_conn: sqlite3.Connection,
    query_vec: list[float],
    limit: int,
) -> list[dict]:
    """sqlite-vec 标量函数 vec_distance_cosine 检索。

    cosine distance ∈ [0, 2]；相似度 = 1 - distance（裁剪到 [0, 1]）。
    """
    query_blob = _serialize_vector(query_vec)
    sql = """
        SELECT m.memory_id, m.content, m.priority, m.updated_at,
               vec_distance_cosine(v.embedding, ?) AS distance
        FROM memory_vectors v
        JOIN memories m ON m.memory_id = v.memory_id
        WHERE m.status != 'rejected'
        ORDER BY distance ASC
        LIMIT ?
    """
    cur = mem_conn.execute(sql, (query_blob, limit))
    results: list[dict] = []
    for r in cur.fetchall():
        distance = float(r["distance"])
        # cosine distance ∈ [0, 2] → similarity ∈ [-1, 1]，裁剪到 [0, 1]
        similarity = max(0.0, 1.0 - distance)
        results.append({
            "memory_id": r["memory_id"],
            "content": r["content"],
            "priority": r["priority"],
            "updated_at": r["updated_at"],
            "score": similarity,
        })
    return results


# ---------- numpy 降级路径 ----------

def _numpy_cosine_search(
    mem_conn: sqlite3.Connection,
    query_vec: list[float],
    limit: int,
) -> list[dict]:
    """内存 numpy 余弦相似检索（sqlite-vec 不可用时降级）。"""
    import numpy as np

    cur = mem_conn.execute(
        """
        SELECT v.memory_id, v.embedding, v.dims,
               m.content, m.priority, m.updated_at
        FROM memory_vectors v
        JOIN memories m ON m.memory_id = v.memory_id
        WHERE m.status != 'rejected'
        """
    )
    rows = cur.fetchall()
    if not rows:
        return []

    q = np.asarray(query_vec, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []

    scored: list[dict] = []
    for r in rows:
        try:
            vec = np.frombuffer(r["embedding"], dtype=np.float32)
        except Exception:
            continue
        if vec.shape[0] != q.shape[0]:
            # 维度不一致跳过（不同模型 embedding 不混检）
            continue
        v_norm = np.linalg.norm(vec)
        if v_norm == 0:
            continue
        # 余弦相似 = dot(a, b) / (|a| * |b|)
        cos_sim = float(np.dot(q, vec) / (q_norm * v_norm))
        # 裁剪到 [0, 1]
        cos_sim = max(0.0, min(1.0, cos_sim))
        scored.append({
            "memory_id": r["memory_id"],
            "content": r["content"],
            "priority": r["priority"],
            "updated_at": r["updated_at"],
            "score": cos_sim,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


# ---------- 序列化 ----------

def _serialize_vector(vec: list[float]) -> bytes:
    """序列化向量为 BLOB（float32 little-endian）。"""
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_vector(blob: bytes) -> list[float]:
    """反序列化 BLOB 为向量。"""
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


# ---------- 场景（L2）向量：对称记忆层（2026-08-07 v5） ----------

def upsert_scene_vector(
    mem_conn: sqlite3.Connection,
    scene_id: str,
    text: str,
    cfg: dict,
    client: httpx.Client | None = None,
) -> bool:
    """生成并存储场景 embedding（scene_vectors 表）。

    返回 True 成功，False 失败（embeddings 不可达）。
    """
    vec = embed(text, cfg, client=client)
    if vec is None:
        return False
    search_cfg = cfg.get("search", {}) or {}
    vec_cfg = search_cfg.get("vector", {}) or {}
    model = vec_cfg.get("model", _DEFAULT_EMBED_MODEL)
    blob = _serialize_vector(vec)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mem_conn.execute(
        """
        INSERT INTO scene_vectors (scene_id, embedding, model, dims, embedded_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(scene_id) DO UPDATE SET
          embedding=excluded.embedding, model=excluded.model,
          dims=excluded.dims, embedded_at=excluded.embedded_at
        """,
        (scene_id, blob, model, len(vec), now),
    )
    mem_conn.commit()
    return True


def scene_vector_search(
    mem_conn: sqlite3.Connection,
    query_vec: list[float],
    limit: int = 10,
) -> list[dict]:
    """场景向量检索（scene_vectors JOIN scenes，sqlite-vec 或 numpy 降级）。

    返回 [{scene_id, title, content, heat, score}]（score=相似度 0-1，DESC）。
    """
    try_load_vec_extension(mem_conn)
    if _VEC_EXTENSION_LOADED:
        try:
            return _sqlite_vec_scene_search(mem_conn, query_vec, limit)
        except Exception as e:
            logger.warning("sqlite-vec 场景检索失败，降级 numpy: %s", e)
    return _numpy_cosine_scene_search(mem_conn, query_vec, limit)


def _sqlite_vec_scene_search(
    mem_conn: sqlite3.Connection,
    query_vec: list[float],
    limit: int,
) -> list[dict]:
    query_blob = _serialize_vector(query_vec)
    sql = """
        SELECT s.scene_id, s.title, s.content, s.heat,
               vec_distance_cosine(v.embedding, ?) AS distance
        FROM scene_vectors v
        JOIN scenes s ON s.scene_id = v.scene_id
        WHERE s.status = 'active'
        ORDER BY distance ASC
        LIMIT ?
    """
    cur = mem_conn.execute(sql, (query_blob, limit))
    return [
        {
            "scene_id": r["scene_id"],
            "title": r["title"],
            "content": r["content"],
            "heat": r["heat"],
            "score": max(0.0, min(1.0, 1.0 - r["distance"])),
        }
        for r in cur.fetchall()
    ]


def _numpy_cosine_scene_search(
    mem_conn: sqlite3.Connection,
    query_vec: list[float],
    limit: int,
) -> list[dict]:
    import numpy as np

    cur = mem_conn.execute(
        """
        SELECT v.scene_id, v.embedding, v.dims,
               s.title, s.content, s.heat
        FROM scene_vectors v
        JOIN scenes s ON s.scene_id = v.scene_id
        WHERE s.status = 'active'
        """
    )
    rows = cur.fetchall()
    if not rows:
        return []

    q = np.asarray(query_vec, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []
    q = q / q_norm

    scored: list[tuple[float, dict]] = []
    for r in rows:
        v = np.asarray(_deserialize_vector(r["embedding"]), dtype=np.float32)
        norm = np.linalg.norm(v)
        if norm == 0:
            continue
        sim = float(np.dot(q, v / norm))
        scored.append((
            sim,
            {
                "scene_id": r["scene_id"],
                "title": r["title"],
                "content": r["content"],
                "heat": r["heat"],
                "score": max(0.0, min(1.0, sim)),
            },
        ))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:limit]]
