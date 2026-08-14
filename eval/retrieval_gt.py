"""eval/retrieval_gt.py：检索评测语料 + ground truth 派生。

职责（#32 RRF 网格搜索接入）：
- `build_corpus`：把评测用例的 GT 记忆落进 eval DB（严格时序），产出可检索语料
- `derive_queries`：从用例派生检索查询 + 相关文档集
- `to_ground_truth`：转成 `RRFGridSearch.search` 需要的 `{query: [relevant_ids]}`

三条硬约定（违反任何一条都会静默产生错误评测结论）：

1. **落库顺序不可颠倒**：
   `init_databases` → `import_registry` → `init_fts` → `insert_memory`×N → `upsert_memory_vector`×N。
   `memories_fts` 是 external-content 表，靠 AFTER INSERT 触发器同步。
   若 `init_fts` 晚于 `insert_memory`，已有行**不会**回填进 FTS 索引，
   BM25 全部召回为空——**不报错**，只是所有 NDCG 归零，是最危险的失败模式。
   本模块只负责 `init_fts → insert → upsert_vector` 三步，前两步由 runner 保证。

2. **memory_id 确定性**：`f"{case_id}#{idx}"`（如 `eval-024#0`）。
   禁用 `uuid4()`——否则两次 run 的 memory_id 全变，report.json 的 rrf 段
   永远 diff 非空，可复现性验收永远失败。

3. **GT 派生用对话正文**（`gt_mode="message"`，默认）：
   GT 记忆 content 中位数仅 19.5 字，用「content 前 N 字」当 query
   ≈ 拿答案去搜答案，是最退化的形态（NDCG 虚高且无区分度）。
   因此默认用**用例对话消息正文**（剥掉 `[msg#N] <ts> <role>:` 头行）当 query，
   `relevant` = 同用例全部 GT 记忆。`gt_mode="content"` 仅作对照备选。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from eval.models import EvalCase

logger = logging.getLogger("eval.retrieval_gt")

# 合法 GT 派生模式
VALID_GT_MODES = ("message", "content")

# `content` 模式下截取的查询字符数（对照用；架构师实证该模式最退化）
CONTENT_QUERY_CHARS = 60

# 对话头行：`[msg#1] 2026-01-01T10:00:00Z user:`
_MSG_HEADER_RE = re.compile(r"^\s*\[msg#\d+\]")

# 语料落库固定时间戳（保证两次 run 的 DB 逐字节可复现）
FIXED_TS = "2026-01-01T00:00:00Z"

# ── banner_reason 取值 ──
#
# `vector_available=False` 时必须能回答「为什么不可用」，否则报告只剩一个
# 光秃秃的 ❌，读者无法区分「显式跳过」「端点挂了」「只嵌上了一半」。
# 前三个是固定串，后两个是带数字的动态串（见 `_vector_banner_reason`）：
#   - `vector_partial_{done}/{total}`      部分覆盖（PRD 明令不算可用）
#   - `vector_unavailable_0/{total}`       一条都没成功
BANNER_OK = ""                                   # 100% 覆盖，向量通路可用
BANNER_EMPTY_CORPUS = "empty_corpus"             # 语料为空，无向量可言
BANNER_SKIPPED_BY_FLAG = "vector_skipped_by_flag"  # --rrf-skip-vector 显式跳过
BANNER_NO_CFG = "vector_no_cfg"                  # 无 cfg，拿不到 embeddings 端点


@dataclass
class RetrievalQuery:
    """单条检索查询 + 其相关文档集。"""

    case_id: str = ""
    query: str = ""
    relevant_ids: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)


@dataclass
class RetrievalCorpus:
    """检索评测语料（已落库）+ 派生查询集。

    ★ `vector_available` 的语义（PRD 硬约定，不可放宽）：
    **仅当向量覆盖率 100%** 时为 True。部分覆盖一律 False。

    理由：部分覆盖下 `memory_vectors` 只有一个子集有行，
    `vector_search` 只可能召回这个子集，另一部分记忆在向量路上**永久不可见**。
    此时两路召回的可比性已经被破坏，NDCG 会随「这次嵌上了多少条」上下漂移
    （实测 84/84 → 0.9546，41/84 → 0.5691），把它当「向量通路可用」上报，
    等于用一个随机数当评测基线。
    """

    memory_ids: list[str] = field(default_factory=list)
    queries: list[RetrievalQuery] = field(default_factory=list)
    gt_mode: str = "message"
    vector_available: bool = False
    vector_count: int = 0
    skipped_dimensions: list[str] = field(default_factory=list)
    banner_reason: str = BANNER_OK          # 向量不可用的原因（供 reporter 渲染）
    vector_failed_at: int | None = None     # 熔断发生在第几条（0-based），未熔断为 None
    embed_cache_stats: dict = field(default_factory=dict)  # 缓存命中统计

    @property
    def size(self) -> int:
        """语料规模（落库记忆条数）。"""
        return len(self.memory_ids)

    @property
    def vector_coverage(self) -> float:
        """向量覆盖率 = vector_count / size（空语料返回 0.0）。"""
        if not self.memory_ids:
            return 0.0
        return round(self.vector_count / len(self.memory_ids), 4)


# ── 内部工具 ──

def _msg_text(conversation: str) -> str:
    """剥离 `[msg#N] <ts> <role>:` 头行，返回纯消息正文。

    输入形如::

        [msg#1] 2026-01-01T10:00:00Z user:
          我叫张明，在深圳做后端开发，用 Python 和 Go

    返回 ``"我叫张明，在深圳做后端开发，用 Python 和 Go"``。

    多轮对话的多段正文用单空格拼接（`_build_fts_query` 按空白切 token，
    换行与空格等价，这里取空格便于日志/报告直读）。
    """
    if not conversation:
        return ""
    body: list[str] = []
    for line in conversation.splitlines():
        if _MSG_HEADER_RE.match(line):
            continue
        stripped = line.strip()
        if stripped:
            body.append(stripped)
    return " ".join(body)


def _content_text(case: EvalCase) -> str:
    """`content` 模式：拼接该用例全部 GT 记忆 content，截前 N 字。"""
    joined = " ".join(m.content.strip() for m in case.expected_l1.memories if m.content.strip())
    return joined[:CONTENT_QUERY_CHARS]


def memory_id_for(case_id: str, index: int) -> str:
    """确定性 memory_id：`{case_id}#{index}`（禁用 uuid4）。"""
    return f"{case_id}#{index}"


def vector_banner_reason(
    total: int,
    vector_count: int,
    enable_vector: bool,
    has_cfg: bool,
) -> str:
    """判定 `banner_reason`（`""` 表示向量通路完全可用）。

    判定顺序即优先级：语料为空 > 显式跳过 > 无 cfg > 全失败 > 部分覆盖 > OK。
    """
    if total <= 0:
        return BANNER_EMPTY_CORPUS
    if not enable_vector:
        return BANNER_SKIPPED_BY_FLAG
    if not has_cfg:
        return BANNER_NO_CFG
    if vector_count <= 0:
        return f"vector_unavailable_0/{total}"
    if vector_count < total:
        return f"vector_partial_{vector_count}/{total}"
    return BANNER_OK


def _registry_dimension_ids(mem_conn: sqlite3.Connection) -> set[str]:
    """读取 eval DB 中已注册的维度 id 集合（用于过滤 FK 非法标签）。"""
    try:
        cur = mem_conn.execute("SELECT id FROM dimension_registry")
        return {row[0] for row in cur.fetchall()}
    except sqlite3.Error as e:
        logger.warning("读取 dimension_registry 失败，跳过维度过滤: %s", e)
        return set()


# ── 公开 API ──

def derive_queries(
    cases: Iterable[EvalCase],
    mode: str = "message",
) -> list[RetrievalQuery]:
    """从评测用例派生检索查询集。

    - ``mode="message"``（默认）：query = 用例对话消息正文（剥头行）
    - ``mode="content"``：query = 同用例 GT 记忆 content 拼接后的前 60 字（对照用）

    ``relevant_ids`` 恒为「同用例的全部 GT 记忆 id」，保证非空
    （`_compute_ndcg` 对空相关集返回 1.0，会污染均值，必须避免）。

    无 GT 记忆或 query 为空的用例直接跳过。
    """
    if mode not in VALID_GT_MODES:
        raise ValueError(f"gt_mode 非法: {mode!r}，合法值 {VALID_GT_MODES}")

    queries: list[RetrievalQuery] = []
    for case in cases:
        memories = case.expected_l1.memories
        if not memories:
            logger.debug("用例 %s 无 GT 记忆，跳过", case.case_id)
            continue

        query_text = _msg_text(case.conversation) if mode == "message" else _content_text(case)
        query_text = query_text.strip()
        if not query_text:
            logger.debug("用例 %s 派生 query 为空（mode=%s），跳过", case.case_id, mode)
            continue

        relevant_ids = [memory_id_for(case.case_id, i) for i in range(len(memories))]
        dims: list[str] = []
        for mem in memories:
            for d in mem.dimensions:
                if d not in dims:
                    dims.append(d)

        queries.append(RetrievalQuery(
            case_id=case.case_id,
            query=query_text,
            relevant_ids=relevant_ids,
            dimensions=dims,
        ))

    logger.info("派生检索查询 %d 条（mode=%s）", len(queries), mode)
    return queries


def to_ground_truth(queries: Iterable[RetrievalQuery]) -> dict[str, list[str]]:
    """转成 `RRFGridSearch.search` 需要的 `{query_text: [relevant_ids]}`。

    query 文本重复时合并相关集（去重保序），避免后一条静默覆盖前一条。
    """
    gt: dict[str, list[str]] = {}
    for q in queries:
        bucket = gt.setdefault(q.query, [])
        for mid in q.relevant_ids:
            if mid not in bucket:
                bucket.append(mid)
    return gt


def build_corpus(
    mem_conn: sqlite3.Connection,
    cases: Iterable[EvalCase],
    cfg: dict | None = None,
    client: Any | None = None,
    enable_vector: bool = True,
    gt_mode: str = "message",
) -> RetrievalCorpus:
    """把 GT 记忆落进 eval DB 并派生查询集。

    严格时序（**顺序不可颠倒**，见模块 docstring 约定 1）::

        init_fts(mem_conn)                    # ① 建 FTS5 虚拟表 + 触发器
        insert_memory(...) × N                # ② 逐条落库（触发器同步进 FTS）
        upsert_memory_vector(...) × N         # ③ 逐条补 embedding

    ``mem_conn`` 必须已由调用方完成 `init_databases` + `import_registry`
    （维度注册表缺失会导致 memory_tags 写入触发 FK 错误）。

    ★ 向量嵌入的两条硬规则：

    1. **熔断：任意一条 embed 失败即停**（不只是首条）。
       端点一旦开始失败，剩余几十条逐条重试只会拖慢一个数量级，
       且会制造「一半有向量一半没有」的部分覆盖语料——比完全没有向量更危险。

    2. **`vector_available` 仅在 100% 覆盖时为 True**（PRD 硬约定）。
       `vector_count < len(memory_ids)` ⇒ False + `banner_reason="vector_partial_x/y"`。

    返回 `RetrievalCorpus`。
    """
    cases = list(cases)

    # ① init_fts —— 必须早于任何 insert_memory
    from sgme.data.search import init_fts
    init_fts(mem_conn)

    from sgme.data import memory_dao

    valid_dims = _registry_dimension_ids(mem_conn)
    memory_ids: list[str] = []
    texts: list[str] = []
    skipped_dimensions: list[str] = []

    # ② insert_memory × N
    for case in cases:
        for idx, gt_mem in enumerate(case.expected_l1.memories):
            mid = memory_id_for(case.case_id, idx)
            dims = list(gt_mem.dimensions)
            if valid_dims:
                kept = [d for d in dims if d in valid_dims]
                for d in dims:
                    if d not in valid_dims and d not in skipped_dimensions:
                        skipped_dimensions.append(d)
                dims = kept
            try:
                memory_dao.insert_memory(
                    mem_conn,
                    content=gt_mem.content,
                    memory_type=gt_mem.memory_type,
                    priority=gt_mem.priority,
                    time_velocity=gt_mem.time_velocity,
                    ttl_days=None,
                    dimension_ids=dims,
                    sources=None,
                    agent_tag="eval",
                    memory_id=mid,
                    created_at=FIXED_TS,
                    updated_at=FIXED_TS,
                )
            except Exception as e:
                logger.error("落库失败 %s: %s", mid, e)
                continue
            memory_ids.append(mid)
            texts.append(gt_mem.content)

    if skipped_dimensions:
        logger.warning(
            "以下维度不在 eval DB 注册表中，已从标签中剔除: %s",
            ", ".join(sorted(skipped_dimensions)),
        )

    # ③ upsert_memory_vector × N
    total = len(memory_ids)
    vector_count = 0
    vector_failed_at: int | None = None

    if enable_vector and cfg:
        from sgme.data.search import vector as vector_mod
        for i, (mid, text) in enumerate(zip(memory_ids, texts)):
            ok = vector_mod.upsert_memory_vector(mem_conn, mid, text, cfg, client)
            if ok:
                vector_count += 1
                continue
            # ★ 熔断：任意一条失败即停（不是只看首条）
            vector_failed_at = i
            logger.warning(
                "embeddings 嵌入在第 %d/%d 条失败（memory_id=%s），已成功 %d 条；"
                "立即熔断，跳过剩余 %d 条。部分覆盖语料不可用于评测："
                "vector_available 将置 False，RRF 退化为单路 BM25，"
                "rrf_k 对排序无任何影响",
                i + 1, total, mid, vector_count, total - i,
            )
            break
    elif not enable_vector:
        logger.info("--rrf-skip-vector：显式跳过向量嵌入，RRF 退化单路")
    else:
        logger.info("build_corpus 未收到 cfg，无法解析 embeddings 端点，跳过向量嵌入")

    # ★ PRD 硬约定：仅 100% 覆盖才算「向量通路可用」，部分覆盖一律 False
    vector_available = bool(total) and vector_count == total
    banner_reason = vector_banner_reason(
        total=total,
        vector_count=vector_count,
        enable_vector=enable_vector,
        has_cfg=bool(cfg),
    )
    if vector_available and banner_reason:
        # 防御性自检：两处判定必须同号，不同号说明逻辑被改坏了
        logger.error(
            "内部不一致：vector_available=True 但 banner_reason=%r，按不可用处理",
            banner_reason,
        )
        vector_available = False

    from eval import embed_cache as embed_cache_mod
    from sgme.data.search import vector as vector_mod_stats
    active_cache = vector_mod_stats.get_embed_cache()
    cache_stats: dict = {}
    if isinstance(active_cache, embed_cache_mod.EmbedCache):
        cache_stats = active_cache.stats_dict()

    corpus = RetrievalCorpus(
        memory_ids=memory_ids,
        queries=derive_queries(cases, mode=gt_mode),
        gt_mode=gt_mode,
        vector_available=vector_available,
        vector_count=vector_count,
        skipped_dimensions=sorted(skipped_dimensions),
        banner_reason=banner_reason,
        vector_failed_at=vector_failed_at,
        embed_cache_stats=cache_stats,
    )
    logger.info(
        "检索语料就绪: 记忆=%d 查询=%d 向量=%d/%d(覆盖率=%.4f available=%s reason=%r) "
        "gt_mode=%s 缓存=%s",
        corpus.size, len(corpus.queries), vector_count, total,
        corpus.vector_coverage, corpus.vector_available, banner_reason,
        gt_mode, cache_stats or "未启用",
    )
    return corpus
