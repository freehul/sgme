"""search 层：FTS5 BM25 + 向量检索 + RRF 融合（§3 / §16.4）。

- init_fts: 创建 FTS5 虚拟表 + 同步触发器（幂等）+ content_seg 口径保障
- recall_routes: 双路原始召回（BM25 + 向量），**不做 RRF 融合**
- search_memories: recall_routes + RRF 融合 + rank/source/routes/dimensions/trace 装饰

中文检索分词 v0.3（分层职责）：
- storage 管 `content_seg` 列**存在**（MEMORY_DDL + _migrate_mem_content_seg）
- search 管列**内容**（_ensure_fts_ready 全量回填 + FTS 重建 + fts_meta marker）
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import httpx

from sgme.data.search import rrf as rrf_mod
from sgme.data.search import vector as vector_mod
from sgme.data.search import stoplist as stoplist_mod
from sgme.segment import current_segmenter_id, segment, segment_terms

logger = logging.getLogger("sgme.data.search")


# FTS5 虚拟表 + 同步触发器（外部内容表，content='memories'；索引 content_seg 分词列）
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content_seg,
    memory_id UNINDEXED,
    content='memories',
    content_rowid='rowid'
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content_seg, memory_id)
    VALUES (new.rowid, new.content_seg, new.memory_id);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content_seg, memory_id)
    VALUES ('delete', old.rowid, old.content_seg, old.memory_id);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content_seg, memory_id)
    VALUES ('delete', old.rowid, old.content_seg, old.memory_id);
    INSERT INTO memories_fts(rowid, content_seg, memory_id)
    VALUES (new.rowid, new.content_seg, new.memory_id);
END;
"""

# fts_meta KV 表：持久化分词器口径标识（缺口 A：口径漂移必须可观测）
FTS_META_DDL = """
CREATE TABLE IF NOT EXISTS fts_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# ---------- scenes（L2）FTS：对称记忆层方案（2026-08-07 v5） ----------

SCENES_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS scenes_fts USING fts5(
    content_seg,
    scene_id UNINDEXED,
    content='scenes',
    content_rowid='rowid'
);
"""

SCENES_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS scenes_ai AFTER INSERT ON scenes BEGIN
    INSERT INTO scenes_fts(rowid, content_seg, scene_id)
    VALUES (new.rowid, new.content_seg, new.scene_id);
END;
CREATE TRIGGER IF NOT EXISTS scenes_ad AFTER DELETE ON scenes BEGIN
    INSERT INTO scenes_fts(scenes_fts, rowid, content_seg, scene_id)
    VALUES ('delete', old.rowid, old.content_seg, old.scene_id);
END;
CREATE TRIGGER IF NOT EXISTS scenes_au AFTER UPDATE ON scenes BEGIN
    INSERT INTO scenes_fts(scenes_fts, rowid, content_seg, scene_id)
    VALUES ('delete', old.rowid, old.content_seg, old.scene_id);
    INSERT INTO scenes_fts(rowid, content_seg, scene_id)
    VALUES (new.rowid, new.content_seg, new.scene_id);
END;
"""


def init_scenes_fts(wiki_conn: sqlite3.Connection) -> None:
    """初始化 scenes FTS5 虚拟表与同步触发器（幂等，对称 init_fts）。

    - 断言 content_seg 列存在（缺列 = storage 迁移未执行，抛清晰错误）
    - 首建/口径漂移：摘触发器 → 回填 content_seg → 建 FTS → 写 fts_meta marker
    - 失败 WARNING + 降级（search_scenes 的 LIKE 兜底），不炸调用方
    """
    cols = [r[1] for r in wiki_conn.execute("PRAGMA table_info(scenes)").fetchall()]
    if "content_seg" not in cols:
        raise RuntimeError(
            "storage 迁移未执行：scenes 表缺 content_seg 列（_migrate_wiki_scene_seg）"
        )
    wiki_conn.executescript(FTS_META_DDL)
    wiki_conn.commit()

    runtime_seg = current_segmenter_id()
    marker_row = wiki_conn.execute(
        "SELECT value FROM fts_meta WHERE key='segmenter_scenes'"
    ).fetchone()
    stored_seg = marker_row["value"] if marker_row else None
    fts_ok = _scenes_fts_indexes_content_seg(wiki_conn)

    if stored_seg == runtime_seg and fts_ok:
        wiki_conn.executescript(SCENES_FTS_DDL)
        wiki_conn.executescript(SCENES_FTS_TRIGGERS)
        wiki_conn.commit()
        return

    try:
        wiki_conn.execute("BEGIN")
        for trig in ("scenes_ai", "scenes_ad", "scenes_au"):
            wiki_conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        wiki_conn.execute("DROP TABLE IF EXISTS scenes_fts")
        rows = wiki_conn.execute(
            "SELECT rowid, title, content FROM scenes"
        ).fetchall()
        for row in rows:
            seg_text = segment(f"{row['title']} {row['content']}")
            wiki_conn.execute(
                "UPDATE scenes SET content_seg=? WHERE rowid=?",
                (seg_text, row["rowid"]),
            )
        wiki_conn.executescript(SCENES_FTS_DDL)
        wiki_conn.executescript(SCENES_FTS_TRIGGERS)
        wiki_conn.execute("INSERT INTO scenes_fts(scenes_fts) VALUES('rebuild')")
        wiki_conn.execute(
            "INSERT INTO fts_meta (key, value) VALUES ('segmenter_scenes', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (runtime_seg,),
        )
        wiki_conn.commit()
        logger.info("scenes FTS 已构建：reason=%s, segmenter=%s",
                    "首建" if stored_seg is None else "口径漂移", runtime_seg)
    except Exception as e:
        wiki_conn.rollback()
        logger.warning("scenes FTS 构建失败：search_scenes 退化为 LIKE 兜底: %s", e)


def _scenes_fts_indexes_content_seg(conn: sqlite3.Connection) -> bool:
    """检查 scenes_fts 是否索引 content_seg 列（旧 content 列 ⇒ False）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='scenes_fts'"
    ).fetchone()
    if row is None or not row["sql"]:
        return False
    return "content_seg" in row["sql"]


class FtsSchemaError(RuntimeError):
    """缺 content_seg 列（storage 迁移未执行）——init_fts 必须上浮的部署错误。"""


class FtsRebuildError(RuntimeError):
    """FTS 重建失败——init_fts 捕获后 WARNING + 降级 LIKE，不炸调用方。

    QA Bug 2 修复：与「缺列 FtsSchemaError」用不同异常类型区分，避免
    init_fts 把重建期 RuntimeError 误判为「缺列必须上浮」而炸掉调用方。
    """


def init_fts(mem_conn: sqlite3.Connection) -> None:
    """初始化 FTS5 虚拟表与同步触发器（幂等）。

    必须在 memories 表已建好后调用（connect_memory 之后）。

    内部委托 `_ensure_fts_ready`：
    - 断言 `content_seg` 列存在（缺列 = storage 迁移未执行，**不做 ALTER**，
      抛 FtsSchemaError 上浮）
    - 比对 `fts_meta.segmenter` vs `current_segmenter_id()`，口径不一致/首建即
      全量回填 content_seg + 重建 FTS + 写 marker（缺口 A/B/C 闭环）
    - 重建失败抛 FtsRebuildError → 本函数 WARNING + 降级 LIKE（不炸调用方）
    """
    try:
        _ensure_fts_ready(mem_conn)
    except FtsRebuildError:
        # 重建失败 → WARNING + 降级 LIKE（不炸调用方）；具体原因已在上游 WARNING
        logger.warning("FTS 重建失败，search 退化为 LIKE 兜底")
    except sqlite3.OperationalError:
        # FTS5 不可用（如 SQLite 无 FTS5 模块）→ 降级（search 退化为 LIKE）
        logger.warning("FTS5 不可用，search 退化为 LIKE 兜底")
    except FtsSchemaError:
        # content_seg 列缺失 = storage 迁移未执行的部署错误，必须上浮
        raise
    except Exception:
        # 未知异常兜底：不炸调用方，search 退化为 LIKE（可观测性靠 WARNING）
        logger.warning("FTS 就绪检查未完成，search 退化为 LIKE 兜底")


def _fts_indexes_content_seg(mem_conn: sqlite3.Connection) -> bool:
    """检查现有 memories_fts 是否已索引 content_seg 列（旧 content 列 ⇒ False）。"""
    row = mem_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if row is None or not row["sql"]:
        return False
    return "content_seg" in row["sql"]


def _ensure_fts_ready(mem_conn: sqlite3.Connection) -> None:
    """保证 FTS5 就绪：列存在断言 + fts_meta marker + 口径漂移强制重建（幂等）。

    分层职责（缺口 C 修复）：列的存在归 storage（T-B），本函数只管理列内容与
    FTS 索引——绝不做 ALTER 补列，缺列即抛清晰错误。
    """
    # 1. 断言 content_seg 列存在（storage 迁移未执行时抛清晰错误）
    cols = [r[1] for r in mem_conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "content_seg" not in cols:
        raise FtsSchemaError(
            "storage 迁移未执行：memories 表缺 content_seg 列。"
            "请先升级 sgme.data.db（SCHEMA_VERSION=4 / _migrate_mem_content_seg）"
        )

    # 2. 建 fts_meta KV 表（持久化 segmenter 标识）
    mem_conn.executescript(FTS_META_DDL)
    mem_conn.commit()

    # 3. 判定是否需重建：库内无 marker / marker != runtime / FTS 仍索引旧列
    runtime_seg = current_segmenter_id()
    marker_row = mem_conn.execute(
        "SELECT value FROM fts_meta WHERE key='segmenter'"
    ).fetchone()
    stored_seg = marker_row["value"] if marker_row else None
    fts_ok = _fts_indexes_content_seg(mem_conn)

    if stored_seg == runtime_seg and fts_ok:
        # 口径一致且索引列正确 → 幂等建表（不重建）
        mem_conn.executescript(FTS_DDL)
        mem_conn.executescript(FTS_TRIGGERS)
        mem_conn.commit()
        return

    reason = (
        "首建" if stored_seg is None
        else "口径漂移" if stored_seg != runtime_seg
        else "索引列旧"
    )

    # 4. 重建分支：回填 content_seg + 重建 FTS + 写 marker（异常显式 WARNING）
    try:
        # ★ 必须先摘旧触发器 + 旧 FTS 表，再回填（QA Bug 1）：
        # 旧 memories_au 引用 new.content，若回填 UPDATE 触发它，会对「不在索引
        # 中的行」做 'delete' 特写 → sqlite3.DatabaseError: database disk image is
        # malformed → 失同步 v3 库（晚 init）重建永久失败、marker 永不写入。
        # 先 DROP 保证回填期间零触发器，重建后由新触发器接管。
        mem_conn.execute("BEGIN")
        for trig in ("memories_ai", "memories_ad", "memories_au"):
            mem_conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        mem_conn.execute("DROP TABLE IF EXISTS memories_fts")

        rows = mem_conn.execute(
            "SELECT rowid, content FROM memories"
        ).fetchall()
        for row in rows:
            seg_text = segment(row["content"])
            mem_conn.execute(
                "UPDATE memories SET content_seg=? WHERE rowid=?",
                (seg_text, row["rowid"]),
            )
        mem_conn.executescript(FTS_DDL)
        mem_conn.executescript(FTS_TRIGGERS)
        mem_conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        mem_conn.execute(
            "INSERT INTO fts_meta (key, value) VALUES ('segmenter', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (runtime_seg,),
        )
        mem_conn.commit()
        logger.info(
            "FTS 分词器口径漂移/首建，已重建 content_seg+索引：reason=%s, segmenter=%s",
            reason, runtime_seg,
        )
    except Exception as e:
        mem_conn.rollback()
        logger.warning(
            "FTS 重建失败（reason=%s, segmenter=%s）：search 将退化为 LIKE 兜底",
            reason, runtime_seg, exc_info=True,
        )
        # 用专用异常类型上浮（QA Bug 2）：init_fts 只对 FtsRebuildError 做
        # 「WARNING + 降级 LIKE」，不会与「缺列 FtsSchemaError」混淆而炸调用方。
        raise FtsRebuildError(
            f"FTS 重建失败（reason={reason}, segmenter={runtime_seg}）: {e}"
        ) from e


def recall_routes(
    mem_conn: sqlite3.Connection,
    query: str,
    dimensions: list[str] | None = None,
    match: str = "any",
    limit: int = 10,
    cfg: dict | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """双路原始召回：BM25（含全部降级链）+ 向量（含全部降级链），**不做 RRF 融合**。

    本函数由 `search_memories` 第 83–127 行整块抽出，逐行对应、零逻辑改动。
    抽出目的：让评测套件（eval/）能在**固定的一次召回**上复用多个 `rrf_k`
    做网格搜索，避免每个 k 重复打 embeddings 端点。

    降级链（与 search_memories 原实现完全一致）：
    1. `_build_fts_query` → FTS5 MATCH（有/无 dimensions 两条 SQL）
    2. `sqlite3.OperationalError`（FTS5 不可用）→ `_search_like_fallback`
    3. FTS5 返回空（中文默认 tokenizer 不分词）→ `_search_like_fallback` 兜底
    4. `cfg is None` 或 `search.vector.enabled=False` → 不走向量
    5. `embed` 抛异常 → query_vec=None → 不走向量
    6. `vector_search` 抛异常 → vec_results=[] → 不走向量
    7. vec_results 非空 + dimensions 非空 → `_filter_by_dimensions`

    返回 `(bm25_results, vec_results, routes)`：
    - bm25_results：BM25/LIKE 原始结果（未融合、未装饰）
    - vec_results：向量原始结果（可能为空 = 单路降级）
    - routes：`["bm25"]` / `["bm25","label"]`（+ `"vector"`,`"rrf"` 当 vec_results 非空）

    注：`routes` 含 `"rrf"` 等价于「调用方应当执行 rrf_merge」，
    与原实现「vec_results 非空才融合并追加 routes」的语义严格一致。
    """
    if not query or not query.strip():
        return [], [], []

    # FTS5 查询：用 OR 连接分词后的词（简单空格切分）+ 停用词过滤（T-130）
    fts_query = _build_fts_query(query, use_stoplist=_stoplist_enabled(cfg))
    bm25_results: list[dict] = []

    try:
        if dimensions:
            bm25_results = _search_with_dims(mem_conn, fts_query, dimensions, match, limit)
        else:
            bm25_results = _search_no_dims(mem_conn, fts_query, limit)
    except sqlite3.OperationalError:
        # FTS5 不可用 → 降级 LIKE
        bm25_results = _search_like_fallback(mem_conn, query, dimensions, match, limit)

    # FTS5 无结果（如中文默认 tokenizer 不分词）→ LIKE 兜底
    if not bm25_results:
        bm25_results = _search_like_fallback(mem_conn, query, dimensions, match, limit)

    # 向量检索（cfg 提供且 enabled 时）
    routes = ["bm25", "label"] if dimensions else ["bm25"]
    vec_results: list[dict] = []
    if cfg is not None and _vector_enabled(cfg):
        try:
            query_vec = vector_mod.embed(query, cfg, client=client)
        except Exception as e:
            logger.warning("向量 embed 异常，降级纯 BM25: %s", e)
            query_vec = None
        if query_vec is not None:
            try:
                vec_results = vector_mod.vector_search(mem_conn, query_vec, limit=limit)
            except Exception as e:
                logger.warning("向量检索异常，降级纯 BM25: %s", e)
                vec_results = []
            # 维度过滤（与 BM25 路径一致：match=any 至少命中一个；match=all 全部命中）
            if vec_results and dimensions:
                vec_results = _filter_by_dimensions(mem_conn, vec_results, dimensions, match)
            if vec_results:
                if "vector" not in routes:
                    routes.append("vector")
                if "rrf" not in routes:
                    routes.append("rrf")

    return bm25_results, vec_results, routes


def _graph_candidates(
    mem_conn: sqlite3.Connection,
    bm25_results: list[dict],
    vec_results: list[dict],
    cfg: dict | None,
) -> list[dict]:
    """图召回 v1（ST-38 T-134）：memory_edges 1-hop 邻居**增量**候选。

    - seed = BM25 ∪ 向量原候选的 memory_id；
    - 扩展各 seed 的 1-hop 邻居（edge_dao.neighbors，双向），
      **排除已在原候选里的 seed**——种子进 RRF 会自耦合推高排序（v0.2 设计必答项），
      graph 路只贡献「邻居中不在原候选里的」增量记忆；
    - 邻居得分 = Σ 连到各 seed 的边权重（同邻居多 seed 累加，weight=共现场景数/
      归档链语义）；按得分降序取 ``search.graph.top_n``；
    - 只取 active 记忆（归档/软删不作为候选）。

    Returns:
        按得分降序的 [{memory_id, content, priority, updated_at, score}]；
        图不可用（无 memory_edges 表 / 无边 / 异常）→ []，不影响主检索。
    """
    if not _graph_enabled(cfg):
        return []
    seed_ids = {
        r["memory_id"] for r in bm25_results
    } | {
        r["memory_id"] for r in vec_results
    }
    if not seed_ids:
        return []
    try:
        has_table = mem_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_edges'"
        ).fetchone()
        if not has_table:
            return []
        from sgme.data import edge_dao

        agg: dict[str, float] = {}
        excl = _graph_exclude_relations(cfg)
        rw = _graph_relation_weights(cfg)
        for sid in seed_ids:
            for n in edge_dao.neighbors(
                mem_conn, sid, exclude_relations=excl, relation_weights=rw,
            ):
                agg[n["memory_id"]] = agg.get(n["memory_id"], 0.0) + n["weight"]
        nbrs = [(mid, w) for mid, w in agg.items() if mid not in seed_ids]
        if not nbrs:
            return []
        nbrs.sort(key=lambda x: (-x[1], x[0]))
        nbrs = nbrs[:_graph_top_n(cfg)]
        ids = [mid for mid, _ in nbrs]
        ph = ",".join("?" * len(ids))
        rows = mem_conn.execute(
            f"SELECT memory_id, content, priority, updated_at FROM memories "
            f"WHERE memory_id IN ({ph}) AND status='active'",
            ids,
        ).fetchall()
        by_id = {r["memory_id"]: r for r in rows}
        out: list[dict] = []
        for mid, w in nbrs:
            r = by_id.get(mid)
            if r is None:
                continue  # 非 active / 已归档 → 不作为候选
            out.append({
                "memory_id": mid,
                "content": r["content"],
                "priority": r["priority"],
                "updated_at": r["updated_at"],
                "score": w,
            })
        return out
    except Exception as e:
        logger.warning("图召回候选不可用（该路空结果）: %s", e)
        return []


def search_memories(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    query: str,
    dimensions: list[str] | None = None,
    match: str = "any",
    limit: int = 10,
    include_sources: bool = True,
    cfg: dict | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    """检索：FTS5 BM25 + 维度标签过滤 + 向量检索 + 图召回（ST-38 T-134）+ RRF 融合 + trace。

    - dimensions 为空 → 仅 BM25 全文
    - dimensions 非空 → 标签过滤（match=any: OR; all: AND）+ BM25 排序
    - cfg 提供 + search.vector.enabled → 叠加向量检索 + RRF 融合
    - 向量不可达 → 自动降级纯 BM25
    - trace: memory_sources 展开 → raw_files.path
    - 返回 [{rank, score, source, memory_id, content, dimensions, priority, updated_at, trace, routes}]

    实现：双路召回委托 `recall_routes`（纯重构，对外行为不变），
    本函数只负责 RRF 融合 + rank/source/routes/dimensions/trace 装饰。
    """
    if not query or not query.strip():
        return []

    bm25_results, vec_results, routes = recall_routes(
        mem_conn,
        query,
        dimensions=dimensions,
        match=match,
        limit=limit,
        cfg=cfg,
        client=client,
    )

    # RRF 融合：vec 或 graph 任一非空 ⇔ 进入融合分支（ST-38 T-134 图路并入）
    # - 仅 vec：行为与原实现逐字节等价（graph_results=None）
    # - 仅 graph（向量关闭）：BM25 + 图邻居融合（A/B 纯 BM25 臂即此形态）
    graph_results = _graph_candidates(mem_conn, bm25_results, vec_results, cfg or {})
    graph_active = bool(graph_results)
    results = bm25_results
    if vec_results or graph_results:
        rrf_k = _rrf_k(cfg or {})
        # T-134 A/B：fill_only=True 时图候选 rank 从 len(bm25) 起算（只填空位、
        # 不干预直接命中——解决「多跳受益 vs 单跳噪声」矛盾）
        graph_offset = len(bm25_results) if _graph_fill_only(cfg or {}) else 0
        results = rrf_mod.rrf_merge(
            bm25_results,
            vec_results,
            k=rrf_k,
            graph_results=graph_results or None,
            graph_weight=_graph_weight(cfg or {}),
            graph_rank_offset=graph_offset,
        )
    if graph_active and "graph" not in routes:
        routes.append("graph")

    # ST-39 T-138：有效期间过滤（valid_to 过期不召回；NULL=永久有效零影响；
    # 存量记忆全 NULL → 与 T-138 前行为一致，T-129 基线天然无回归）
    if _valid_period_enabled(cfg or {}):
        results = _filter_expired(mem_conn, results)

    # T-89（2026-08-20）：内容去重 + limit 截断。
    # 1) 去重：同一事实被 L1 重复落库（不同 memory_id、相同 content）时全量召回
    #    会稀释注入（实测注入 10 条记忆 4 对重复）——按 content 保留最优者；
    # 2) 截断：recall_routes 两路各取 limit 条，rrf_merge 按 id 合并不截断
    #    → 融合后最多 2×limit 条，违反调用方 limit 语义（注入 searchLimit=5
    #    却返回 10 条即此 bug）。
    seen_content: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        c = r.get("content")
        if c is not None:
            if c in seen_content:
                continue
            seen_content.add(c)
        deduped.append(r)
    results = deduped[:limit]

    # 附加 trace + dimensions
    for i, r in enumerate(results):
        r["rank"] = i + 1
        r["source"] = "memory"
        r["routes"] = routes
        r["dimensions"] = _get_memory_tags(mem_conn, r["memory_id"])
        if include_sources:
            r["trace"] = _build_trace(mem_conn, session_conn, r["memory_id"])
    return results


def search_scenes(
    mem_conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    cfg: dict | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    """检索 wiki 场景层（L2 叙事文档，契约 §3 三层检索之一）。

    - FTS5 BM25 主路（scenes_fts + jieba 分词，对称记忆层）
    - 降级链：FTS5 不可用 / 空召回 → LIKE 兜底（原实现）
    - 只返回 active 场景（软删除语义：archived 不参与检索）
    - 按 BM25 相关度 + heat DESC 排序
    - cfg/client 预留向量路（PR#8：场景向量 + RRF 融合）
    - 返回 [{rank, source, scene_id, title, content, heat, updated_at, routes}]
    """
    if not query or not query.strip():
        return []

    def _decorate(rows: list) -> list[dict]:
        return [
            {
                "rank": i + 1,
                "source": "wiki_scene",
                "scene_id": r["scene_id"],
                "title": r["title"],
                "content": r["content"],
                "heat": r["heat"],
                "updated_at": r.get("updated_at"),
                "routes": r.get("routes", ["wiki_bm25"]),
            }
            for i, r in enumerate(rows)
        ]

    try:
        fts_query = _build_fts_query(query, use_stoplist=_stoplist_enabled(cfg))
        rows = mem_conn.execute(
            """
            SELECT f.rowid, s.scene_id, s.title, s.content, s.heat,
                   s.status, s.updated_at, bm25(scenes_fts) AS score
            FROM scenes_fts f
            JOIN scenes s ON s.rowid = f.rowid
            WHERE scenes_fts MATCH ? AND s.status = 'active'
            ORDER BY score ASC, s.heat DESC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
        bm25_rows = [dict(r) for r in rows]
    except sqlite3.OperationalError:
        logger.warning("scenes FTS 不可用，降级 LIKE: %s", query[:50])
        bm25_rows = []

    # 向量路（PR#8）：cfg 提供 + vector.enabled → embed + scene_vector_search → RRF
    vec_results: list[dict] = []
    routes = ["wiki_bm25"]
    if cfg and _vector_enabled(cfg):
        query_vec = vector_mod.embed(query, cfg, client=client)
        if query_vec:
            vec_results = vector_mod.scene_vector_search(mem_conn, query_vec, limit)
            if vec_results:
                routes.append("wiki_vector")

    if vec_results and bm25_rows:
        merged = rrf_mod.rrf_merge(bm25_rows, vec_results, k=_rrf_k(cfg), id_key="scene_id")
        # rrf 输出只含聚合键+content——补 title/heat 元数据（从两路原始结果回查）
        meta = {r["scene_id"]: r for r in [*bm25_rows, *vec_results]}
        for m in merged:
            src = meta.get(m["scene_id"], {})
            m["title"] = src.get("title", "")
            m["heat"] = src.get("heat", 0)
        results = _decorate([dict(r, routes=[*routes, "wiki_rrf"]) for r in merged])
        return results
    if vec_results and not bm25_rows:
        # 纯向量命中：向量结果本身按相似度排好
        return _decorate([dict(r, routes=[*routes, "wiki_rrf"]) for r in vec_results])

    if bm25_rows:
        return _decorate([dict(r, routes=routes) for r in bm25_rows])
    # FTS 空召回（中文兜底，对称记忆层）→ LIKE
    logger.debug("scenes FTS 空召回，降级 LIKE: %s", query[:50])

    keyword = query.strip()
    rows = mem_conn.execute(
        """
        SELECT scene_id, title, content, heat, status, updated_at
        FROM scenes
        WHERE status='active'
          AND (title LIKE ? OR content LIKE ?)
        ORDER BY heat DESC, updated_at DESC
        LIMIT ?
        """,
        (f"%{keyword}%", f"%{keyword}%", limit),
    ).fetchall()
    return _decorate([dict(r, routes=["wiki_like"]) for r in rows])


def _vector_enabled(cfg: dict) -> bool:
    """cfg 中 search.vector.enabled 缺省 True。"""
    search_cfg = cfg.get("search", {}) or {}
    vec_cfg = search_cfg.get("vector", {}) or {}
    return bool(vec_cfg.get("enabled", True))


def _stoplist_enabled(cfg: dict | None) -> bool:
    """cfg 中 search.stoplist.enabled 缺省 True（T-130 查询侧停用词过滤开关）。

    cfg 为 None / 缺键 → 默认开启（生产默认行为）。关闭用于 A/B 对照。
    """
    if not cfg:
        return True
    search_cfg = cfg.get("search", {}) or {}
    sl_cfg = search_cfg.get("stoplist", {}) or {}
    return bool(sl_cfg.get("enabled", True))


def _rrf_k(cfg: dict) -> int:
    """cfg 中 search.rrf.k 缺省 60。"""
    search_cfg = cfg.get("search", {}) or {}
    rrf_cfg = search_cfg.get("rrf", {}) or {}
    return int(rrf_cfg.get("k", 60))


def _graph_setting(cfg: dict | None, key: str, default):
    """cfg 中 search.graph.<key> 取值（ST-38 T-134 图召回独立配置键）。"""
    if not cfg:
        return default
    g = (cfg.get("search") or {}).get("graph") or {}
    return g.get(key, default)


def _graph_enabled(cfg: dict | None) -> bool:
    """cfg 中 search.graph.enabled 缺省 True（A/B 双臂对照时关闭）。"""
    return bool(_graph_setting(cfg, "enabled", True))


def _graph_weight(cfg: dict | None) -> float:
    """cfg 中 search.graph.weight 缺省 1.0（graph 路 RRF 贡献权重，独立配置键；
    A/B 实测最优：1.0=与 bm25 rank0 同权）。"""
    return float(_graph_setting(cfg, "weight", 1.0))


def _graph_top_n(cfg: dict | None) -> int:
    """cfg 中 search.graph.top_n 缺省 20（graph 候选上限，防邻居洪泛）。"""
    return int(_graph_setting(cfg, "top_n", 20))


def _graph_fill_only(cfg: dict | None) -> bool:
    """cfg 中 search.graph.fill_only 缺省 True（T-134 A/B 定夺：fill-only 语义，
    图候选只填空位、不干预直接命中——唯一同时满足两项验收的形态）。"""
    return bool(_graph_setting(cfg, "fill_only", True))


def _graph_exclude_relations(cfg: dict | None) -> list[str] | None:
    """T-137：search.graph.exclude_relations（缺省 ["contradicts"]——否定边不参与
    联想召回，矛盾是负信号，纳入会污染结果）。"""
    v = _graph_setting(cfg, "exclude_relations", ["contradicts"])
    return list(v) if isinstance(v, (list, tuple)) and v else None


def _graph_relation_weights(cfg: dict | None) -> dict[str, float] | None:
    """T-137：search.graph.relation_weights（缺省 {"belongs_to": 0.3}——共现边
    尺度压缩：LLM 置信 0-1 vs 场景数 1-N；语义边/supersedes 保持 1.0）。"""
    v = _graph_setting(cfg, "relation_weights", {"belongs_to": 0.3})
    return dict(v) if isinstance(v, dict) and v else None


def _valid_period_enabled(cfg: dict | None) -> bool:
    """T-138：search.valid_period.enabled（缺省 True；存量记忆 valid_to 全 NULL
    时过滤零影响，天然向后兼容）。"""
    if not cfg:
        return True
    vp = (cfg.get("search") or {}).get("valid_period") or {}
    return bool(vp.get("enabled", True))


def _filter_expired(mem_conn: sqlite3.Connection, results: list[dict]) -> list[dict]:
    """T-138：召回后统一过滤 valid_to 已过期记忆（NULL=永久有效，天然兼容）。

    放 RRF 融合后、去重截断前——一处过滤覆盖 bm25/向量/图三路候选，
    避免改分散 SQL（428/623/777/802/846 多处 status 过滤已够分散）。
    ISO 字符串同格式（YYYY-MM-DDTHH:MM:SSZ）字典序 = 时间序。
    """
    if not results:
        return results
    ids = [r["memory_id"] for r in results]
    ph = ",".join("?" * len(ids))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expired = {
        r["memory_id"]
        for r in mem_conn.execute(
            f"SELECT memory_id FROM memories WHERE memory_id IN ({ph}) "
            "AND valid_to IS NOT NULL AND valid_to < ?",
            (*ids, now),
        )
    }
    if not expired:
        return results
    return [r for r in results if r["memory_id"] not in expired]


def _filter_by_dimensions(
    mem_conn: sqlite3.Connection,
    results: list[dict],
    dimensions: list[str],
    match: str,
) -> list[dict]:
    """对向量检索结果按维度标签过滤（与 BM25 路径一致）。

    - match='any'：记忆至少含一个请求维度
    - match='all'：记忆含全部请求维度
    """
    dim_set = set(dimensions)
    filtered: list[dict] = []
    for r in results:
        tags = set(_get_memory_tags(mem_conn, r["memory_id"]))
        if match == "all":
            if dim_set.issubset(tags):
                filtered.append(r)
        else:
            if dim_set & tags:
                filtered.append(r)
    return filtered


def _build_fts_query(query: str, *, use_stoplist: bool = True) -> str:
    """构造 FTS5 查询：分词 + 停用词过滤 + OR 连接（宽松匹配，T-130）。

    查询侧与写入侧共用 `segment()` 口径（中文检索分词 v0.3 §1.3）——
    两层同一函数，否则 FTS 精确 token 匹配必然错位（中文无空格 → 整串 token）。

    T-130 改动：
    1. 分段后按 `stoplist.filter_stopwords` 去掉功能词/高噪词，降低 OR 连接后
       常见词爆炸稀释 BM25 排序（自然语句类 precision 提升）。
    2. 英文清理：去 jieba 可能残留的空格占位 + 小写归一（unicode61 已折叠，
       这里防御性统一），避免「NAS」与「nas」形态不一致。
    3. 全停用词场景（如「谁 和 在」）→ 回退原 token，不直接空召回；
       真空再走 `recall_routes` 的 LIKE 兜底。
    """
    tokens = [t for t in segment(query).split() if t]
    if not tokens:
        return query

    if use_stoplist:
        filtered = stoplist_mod.filter_stopwords(tokens)
        # 英文清理：去空格占位 + 小写（仅作用于含 ASCII 的 token）
        cleaned = [_clean_en_term(t) for t in filtered]
        cleaned = [t for t in cleaned if t]
        # 全停用词 → 回退原 token，避免空召回
        if cleaned:
            tokens = cleaned

    quoted = [f'"{t}"' for t in tokens]
    return " OR ".join(quoted)


def _clean_en_term(term: str) -> str:
    """英文 token 清理：折叠内部空白 + ASCII 小写归一（防御性）。

    jieba 对中英混排偶发残留空格占位（如「NAS server」会切出带空格的片段），
    unicode61 已做大小写折叠，这里再显式统一，保证查询 token 与索引列一致。
    """
    t = " ".join(term.split())  # 折叠多空格/制表符
    if any(ord(c) < 128 for c in t):
        t = t.lower()
    return t


def _search_no_dims(mem_conn: sqlite3.Connection, fts_query: str, limit: int) -> list[dict]:
    sql = """
        SELECT m.memory_id, m.content, m.priority, m.updated_at,
               bm25(memories_fts) AS score
        FROM memories_fts f
        JOIN memories m ON m.rowid = f.rowid
        WHERE memories_fts MATCH ?
          AND m.status != 'rejected'
        ORDER BY score ASC
        LIMIT ?
    """
    cur = mem_conn.execute(sql, (fts_query, limit))
    return [dict(r) for r in cur.fetchall()]


def _search_with_dims(
    mem_conn: sqlite3.Connection,
    fts_query: str,
    dimensions: list[str],
    match: str,
    limit: int,
) -> list[dict]:
    """FTS5 + 维度标签过滤。"""
    placeholders = ",".join("?" * len(dimensions))
    base = f"""
        SELECT m.memory_id, m.content, m.priority, m.updated_at,
               bm25(memories_fts) AS score
        FROM memories_fts f
        JOIN memories m ON m.rowid = f.rowid
        JOIN memory_tags t ON t.memory_id = m.memory_id
        WHERE memories_fts MATCH ?
          AND t.dimension_id IN ({placeholders})
          AND m.status != 'rejected'
    """
    params: list[Any] = [fts_query, *dimensions]
    if match == "all":
        base += " GROUP BY m.memory_id HAVING COUNT(DISTINCT t.dimension_id)=?"
        params.append(len(dimensions))
    else:
        base += " GROUP BY m.memory_id"
    base += " ORDER BY score ASC LIMIT ?"
    params.append(limit)
    cur = mem_conn.execute(base, params)
    return [dict(r) for r in cur.fetchall()]


def _search_like_fallback(
    mem_conn: sqlite3.Connection,
    query: str,
    dimensions: list[str] | None,
    match: str,
    limit: int,
) -> list[dict]:
    """FTS5 不可用/空召回时降级 LIKE 检索：按词 OR（中文检索分词 v0.3 §2）。

    - 用 `segment_terms(query)` 分词，过滤 `len(term) < 2` 的单字噪声，
      取 top 8 词防 SQL 爆炸
    - 每词 `%term%` 子串匹配 OR 连接（2 字词「深圳」→ `%深圳%` 直接命中）
    - 触发守卫不变：仅在 FTS5 返回空时触发（recall_routes 第 112-113 行）
    - `LIMIT min(limit, 20)`，score 占位 0.0（不参与 RRF 的 bm25 排序权重）
    - 全单字/空词时退化为整串 LIKE（旧行为，至少不空手而归）
    """
    terms = [
        t for t in segment_terms(query)
        if len(t) >= 2 and not stoplist_mod.is_stopword(t)
    ][:8]
    if not terms:
        stripped = query.strip()
        terms = [stripped] if stripped else []

    if not terms:
        return []

    like_clauses = " OR ".join(["content LIKE ?"] * len(terms))
    sql = (
        "SELECT memory_id, content, priority, updated_at, 0.0 AS score "
        f"FROM memories WHERE {like_clauses} AND status != 'rejected'"
    )
    params: list[Any] = [f"%{t}%" for t in terms]
    if dimensions:
        placeholders = ",".join("?" * len(dimensions))
        sql += f" AND memory_id IN (SELECT memory_id FROM memory_tags WHERE dimension_id IN ({placeholders}))"
        params.extend(dimensions)
    sql += " LIMIT ?"
    params.append(min(limit, 20))
    cur = mem_conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _get_memory_tags(mem_conn: sqlite3.Connection, memory_id: str) -> list[str]:
    cur = mem_conn.execute(
        "SELECT dimension_id FROM memory_tags WHERE memory_id=? ORDER BY dimension_id",
        (memory_id,),
    )
    return [r["dimension_id"] for r in cur.fetchall()]


def _build_trace(mem_conn: sqlite3.Connection, session_conn: sqlite3.Connection, memory_id: str) -> list[dict]:
    """构建溯源链：memory_sources → raw_files（file_id:seq → path）。"""
    cur = mem_conn.execute(
        "SELECT source_ref, source_type FROM memory_sources WHERE memory_id=?",
        (memory_id,),
    )
    traces: list[dict] = []
    for r in cur.fetchall():
        src_ref = r["source_ref"]
        # source_ref 格式: "file_id:seq"
        file_id = src_ref.split(":")[0] if ":" in src_ref else src_ref
        msg_id = src_ref.split(":", 1)[1] if ":" in src_ref else None
        # 查 raw_files 取 path
        rf = session_conn.execute(
            "SELECT path FROM raw_files WHERE file_id=?", (file_id,)
        ).fetchone()
        traces.append({
            "type": "raw",
            "file_id": file_id,
            "path": rf["path"] if rf else None,
            "msg_id": msg_id,
            "source_ref": src_ref,
            "source_type": r["source_type"],
        })
    return traces
