"""eval/runner.py：评测流水线。

EvalRunner: 初始化干净 eval DB → 对每条用例调用现有引擎（或 mock）→
收集结果 → 调用 metrics 计算 → 返回 EvalResult。

设计依据：docs/design/SGME-评测框架设计-v0.1.md §1.1。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval import loader as eval_loader
from eval import metrics as eval_metrics
from eval.models import (
    CaseResult,
    EvalCase,
    EvalResult,
    EvalSummary,
    L1Metrics,
    L2Metrics,
    RRFMetrics,
)

logger = logging.getLogger("eval.runner")

# 项目根目录（eval/ 与 sgme/ 并列）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# eval 临时数据库默认目录
DEFAULT_EVAL_TMP = PROJECT_ROOT / "eval" / "tmp"

# `rrf_embed_cache` 的哨兵默认值。
# 不能直接用 None 当默认——None 必须保留「显式禁用缓存」的语义。
_EMBED_CACHE_DEFAULT = object()


class EvalRunner:
    """评测流水线：串联 L1/L1.5/L2/模板查询 + 调用 metrics。

    - 零生产污染：eval DB 位于 eval/tmp/ 目录，每次 run 从干净 DB 开始
    - dry_run 模式：mock LLM 输出，验证全链路可运行
    - 正常模式：通过 sgme.engine.* public API 对接提炼链路
    """

    def __init__(
        self,
        cfg: dict,
        prompt_version: str | None = None,
        eval_tmp_dir: Path | str | None = None,
        rrf_gt_mode: str = "message",
        rrf_skip_vector: bool = False,
        rrf_embed_cache: Path | str | None = _EMBED_CACHE_DEFAULT,
    ):
        """初始化评测运行器。

        cfg: SGME 配置字典（可选，dry_run 模式可为空）
        prompt_version: 提示词版本（如 "v001"），dry_run 模式忽略
        eval_tmp_dir: 评测临时目录（默认 eval/tmp/）
        rrf_gt_mode: RRF 检索 GT 派生模式（"message" 对话正文 / "content" GT 内容）
        rrf_skip_vector: 跳过向量嵌入（RRF 退化单路 BM25，用于快速自检）
        rrf_embed_cache: embedding 磁盘缓存库路径。
          默认 `eval/fixtures/embed_cache_v001.sqlite`（已归档进 git ⇒ 离线可复现）；
          传 `None` 显式禁用缓存，每次都真打端点。
        """
        self.cfg = cfg
        self.prompt_version = prompt_version
        self.rrf_gt_mode = rrf_gt_mode
        self.rrf_skip_vector = rrf_skip_vector

        if rrf_embed_cache is _EMBED_CACHE_DEFAULT:
            from eval.embed_cache import DEFAULT_CACHE_PATH
            self.rrf_embed_cache_path: Path | None = Path(DEFAULT_CACHE_PATH)
        elif rrf_embed_cache is None:
            self.rrf_embed_cache_path = None
        else:
            self.rrf_embed_cache_path = Path(rrf_embed_cache)

        if eval_tmp_dir is None:
            self.eval_tmp_dir = Path(DEFAULT_EVAL_TMP)
        else:
            self.eval_tmp_dir = Path(eval_tmp_dir)

        self.mem_conn: sqlite3.Connection | None = None
        self.session_conn: sqlite3.Connection | None = None
        self.wiki_conn: sqlite3.Connection | None = None

    # ── 主入口 ──

    def run_all(
        self,
        cases: list[EvalCase],
        stages: list[str] | None = None,
        dry_run: bool = False,
    ) -> EvalResult:
        """执行全部用例的评测。

        stages: 评测阶段列表 ["l1", "l15", "l2", "rrf"]，默认 ["l1"]
        dry_run: mock LLM 输出模式（无需 LM Studio）

        "rrf" 阶段**独立于 L1**：直接把 GT 记忆落进 eval DB 做检索调参，
        不依赖 L1 提取产物，因此不强制先跑 L1。

        返回 EvalResult（含聚合指标 + 逐用例明细）。
        """
        if stages is None:
            stages = ["l1"]

        run_id = f"eval-{uuid.uuid4().hex[:8]}"
        start_time = time.time()

        logger.info("评测开始: run_id=%s cases=%d stages=%s dry_run=%s",
                    run_id, len(cases), stages, dry_run)

        # 初始化 eval DB
        self._setup_eval_db()

        per_case_results: list[CaseResult] = []
        per_case_l1_metrics: list[L1Metrics] = []
        per_case_l2_metrics: list[L2Metrics] = []

        for case in cases:
            try:
                cr = self.run_one(case, stages, dry_run)
                per_case_results.append(cr)

                # 对每个 case 单独计算 L1 指标（用于聚合）
                if "l1" in stages and hasattr(cr, "_l1_predictions"):
                    l1m = eval_metrics.compute_l1_f1(
                        cr._l1_predictions, case.expected_l1,  # type: ignore[attr-defined]
                    )
                    per_case_l1_metrics.append(l1m)
                    cr.l1_f1 = l1m.dimension_micro_f1
                    cr.strict_match = l1m.strict_match_rate > 0.5
                    cr.dimension_details = [
                        {"dimension_id": d.dimension_id, "f1": d.f1}
                        for d in l1m.per_dimension_f1.values()
                    ]
            except Exception as e:
                logger.error("用例 %s 评测异常: %s", case.case_id, e)
                cr = CaseResult(
                    case_id=case.case_id,
                    difficulty=case.difficulty,
                    error=str(e),
                )
                per_case_results.append(cr)

        # RRF 检索调参阶段（独立于 L1，用 GT 记忆直接建检索语料）
        rrf_metrics: RRFMetrics | None = None
        if "rrf" in stages:
            try:
                rrf_metrics = self._run_rrf(cases)
            except Exception as e:
                logger.error("RRF 阶段异常: %s", e, exc_info=True)
                rrf_metrics = RRFMetrics(
                    conclusion="error",
                    gt_mode=self.rrf_gt_mode,
                    recall_diagnostics={"error": str(e)},
                )

        # 聚合度量
        aggregated_l1 = eval_metrics.aggregate_l1_metrics(per_case_l1_metrics) \
            if per_case_l1_metrics else L1Metrics()

        l1_f1_for_l2 = aggregated_l1.dimension_micro_f1
        aggregated_l2 = eval_metrics.aggregate_l2_metrics(per_case_l2_metrics, l1_f1_for_l2) \
            if per_case_l2_metrics else L2Metrics()

        # 计算 summary
        duration = time.time() - start_time
        p0_status = self._check_p0_targets(aggregated_l1, aggregated_l2)

        summary = EvalSummary(
            total_cases=len(cases),
            passed_p0=all(v == "green" for v in p0_status.values()),
            p0_status=p0_status,
            duration_seconds=round(duration, 2),
        )

        # 清理 eval DB
        self._teardown_eval_db()

        logger.info(
            "评测完成: run_id=%s F1=%.4f Strict=%.4f Profile=%.4f duration=%.1fs",
            run_id,
            aggregated_l1.dimension_micro_f1,
            aggregated_l1.strict_match_rate,
            aggregated_l2.profile_quality,
            duration,
        )

        return EvalResult(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            prompt_versions={"l1_extraction": self.prompt_version or "dry-run"},
            l1=aggregated_l1,
            l2=aggregated_l2,
            rrf=rrf_metrics,
            per_case=per_case_results,
            summary=summary,
        )

    def run_one(
        self,
        case: EvalCase,
        stages: list[str],
        dry_run: bool = False,
    ) -> CaseResult:
        """执行单条用例的评测。

        返回 CaseResult（含 L1 预测记忆引用 _l1_predictions 供聚合）。
        """
        cr = CaseResult(
            case_id=case.case_id,
            difficulty=case.difficulty,
        )

        # L1 提取
        if "l1" in stages:
            predictions, meta = self._run_l1(case, dry_run)
            cr._l1_predictions = predictions  # type: ignore[attr-defined]

            # 记忆匹配统计
            gt_memories = case.expected_l1.memories
            matched, unmatched_preds, unmatched_gts = eval_metrics._match_memories(
                predictions, gt_memories,
            )
            cr.matched_memories = len(matched)
            cr.unmatched_pred = len(unmatched_preds)
            cr.unmatched_gt = len(unmatched_gts)

        return cr

    # ── DB 管理 ──

    def _load_eval_registry(self) -> tuple[list[dict], dict[str, list[str]]]:
        """取维度注册表 + 别名（P0-0A 兜底）。

        `EvalRunner(cfg={})` 场景下 cfg 无 `dimensions`，若直接跳过 import_registry，
        后续 `insert_memory` 写 memory_tags 会因 FK（dimension_registry.id）崩溃。
        因此 cfg 缺失时直接从 `registry/dimensions.yaml` + `registry/aliases.yaml` 读。

        注：`load_dimensions`/`load_aliases` 是纯 YAML 读取，不触碰 data/ 目录。
        """
        dimensions = self.cfg.get("dimensions") if isinstance(self.cfg, dict) else None
        aliases = self.cfg.get("aliases") if isinstance(self.cfg, dict) else None
        if dimensions:
            return list(dimensions), dict(aliases or {})

        try:
            from sgme import config as sgme_config
            dimensions = sgme_config.load_dimensions()
            aliases = sgme_config.load_aliases()
            logger.info(
                "cfg 无 dimensions，从 registry/*.yaml 兜底加载 %d 维 / %d 组别名",
                len(dimensions), len(aliases),
            )
            return list(dimensions), dict(aliases or {})
        except Exception as e:
            logger.warning("registry 兜底加载失败（memory_tags 写入可能触发 FK 错误）: %s", e)
            return [], {}

    def _setup_eval_db(self) -> None:
        """创建 eval 专用数据库（eval/tmp/{memory,session,wiki}.db）。

        文件名与生产一致（v0.7 起 `init_databases` 固定
        memory.db / session.db / wiki.db 三库），
        但目录是 `eval/tmp/`，与生产 `data/` 目录物理隔离。
        """
        self.eval_tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            from sgme.data.db import init_databases
            self.mem_conn, self.session_conn, self.wiki_conn = init_databases(
                self.eval_tmp_dir
            )

            # 导入维度注册表到 eval DB（cfg 缺失时从 registry/*.yaml 兜底）
            from sgme.data import memory_dao
            dimensions, aliases = self._load_eval_registry()
            if dimensions:
                memory_dao.import_registry(self.mem_conn, dimensions, aliases)

            logger.info("eval DB 初始化完成: %s", self.eval_tmp_dir)
        except ImportError:
            # sgme 不可用时（如纯测试环境），创建简单 SQLite 连接
            self.mem_conn = sqlite3.connect(
                str(self.eval_tmp_dir / "memory.db")
            )
            self.session_conn = sqlite3.connect(
                str(self.eval_tmp_dir / "session.db")
            )
            self.wiki_conn = sqlite3.connect(
                str(self.eval_tmp_dir / "wiki.db")
            )
            logger.warning("sgme.storage.db 不可用，使用裸 SQLite 连接")

    def _teardown_eval_db(self) -> None:
        """关闭并清理 eval 数据库（含 WAL 副文件）。"""
        for conn in (self.mem_conn, self.session_conn, self.wiki_conn):
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    logger.warning("关闭 eval DB 连接失败: %s", e)

        # 清理临时 DB 文件（v0.7：init_databases 创建 memory.db + session.db + wiki.db；
        # PRAGMA journal_mode=WAL 会额外产生 -wal / -shm 副文件，不清会跨 run 残留）
        for suffix in (
            "memory.db", "memory.db-wal", "memory.db-shm",
            "session.db", "session.db-wal", "session.db-shm",
            "wiki.db", "wiki.db-wal", "wiki.db-shm",
        ):
            db_path = self.eval_tmp_dir / suffix
            if db_path.exists():
                try:
                    db_path.unlink()
                except Exception as e:
                    logger.warning("清理 eval DB 文件失败 %s: %s", db_path, e)

        self.mem_conn = None
        self.session_conn = None
        self.wiki_conn = None

    # ── RRF 检索调参 ──

    def _setup_retrieval_corpus(self, cases: list[EvalCase]):
        """把 GT 记忆落进 eval DB，产出检索语料 + 查询集。

        前置：`_setup_eval_db()` 已完成 `init_databases` + `import_registry`。
        本方法内部由 `retrieval_gt.build_corpus` 保证
        `init_fts → insert_memory×N → upsert_memory_vector×N` 的严格时序。
        """
        from eval import retrieval_gt

        if self.mem_conn is None:
            raise RuntimeError("eval DB 未初始化，无法构建检索语料")

        return retrieval_gt.build_corpus(
            self.mem_conn,
            cases,
            cfg=self.cfg or None,
            client=None,
            enable_vector=not self.rrf_skip_vector,
            gt_mode=self.rrf_gt_mode,
        )

    def _search_cfg(self, vector_available: bool) -> dict | None:
        """构造传给 `recall_routes` 的 cfg（**浅拷贝，绝不原地改 self.cfg**）。

        向量不可用时显式关闭 `search.vector.enabled`，
        避免每条 query 都去打一次不可达的 embeddings 端点（50 次无谓超时）。
        """
        if not self.cfg:
            return None
        if vector_available:
            return self.cfg
        search_cfg = dict(self.cfg.get("search", {}) or {})
        vec_cfg = dict(search_cfg.get("vector", {}) or {})
        vec_cfg["enabled"] = False
        search_cfg["vector"] = vec_cfg
        return {**self.cfg, "search": search_cfg}

    def _make_query_fn(self, corpus):
        """构造注入 `RRFGridSearch.search` 的 `query_fn`（带双路召回缓存）。

        缓存 key = `(query_text, dims_key, limit)`，value = **两路原始结果**
        `(bm25_results, vec_results)`（不是融合结果——融合依赖 rrf_k，必须每次现算）。
        5 个 rrf_k 复用同一次召回：召回次数从 250 降到 50。
        进程内 dict，run 结束随闭包一起丢弃，不落盘。

        返回的函数满足 search() 的注入契约：去重、降序、≤ RECALL_LIMIT、纯函数、不抛异常。
        缓存对象通过 `query_fn.recall_cache` 暴露，供 `_recall_diagnostics` 事后统计。
        """
        from eval.rrf import RECALL_LIMIT
        from sgme.data.search import recall_routes
        from sgme.data.search.rrf import rrf_merge

        mem_conn = self.mem_conn
        search_cfg = self._search_cfg(corpus.vector_available)
        cache: dict[tuple, tuple[list[dict], list[dict]]] = {}

        # 全语料检索：不按维度过滤（GT 相关集是「同用例全部记忆」，维度过滤会误杀）
        dims: list[str] | None = None
        dims_key: tuple = ()

        def query_fn(query_text: str, params: dict) -> list[str]:
            try:
                rrf_k = int(params.get("rrf_k", 60))
            except (TypeError, ValueError):
                rrf_k = 60

            key = (query_text, dims_key, RECALL_LIMIT)
            if key not in cache:
                try:
                    bm25_results, vec_results, _routes = recall_routes(
                        mem_conn,
                        query_text,
                        dimensions=dims,
                        match="any",
                        limit=RECALL_LIMIT,
                        cfg=search_cfg,
                        client=None,
                    )
                except Exception as e:
                    logger.warning("recall_routes 异常（query=%.40s…）: %s", query_text, e)
                    bm25_results, vec_results = [], []
                cache[key] = (bm25_results, vec_results)

            bm25_results, vec_results = cache[key]
            if vec_results:
                merged = rrf_merge(bm25_results, vec_results, k=rrf_k)
            else:
                # 单路：无可融合的第二路，直接用 BM25 原序（rrf_k 在此天然无影响）
                merged = bm25_results

            ordered: list[str] = []
            seen: set[str] = set()
            for r in merged:
                mid = r.get("memory_id")
                if mid and mid not in seen:
                    seen.add(mid)
                    ordered.append(mid)
                if len(ordered) >= RECALL_LIMIT:
                    break
            return ordered

        query_fn.recall_cache = cache  # type: ignore[attr-defined]
        return query_fn

    @staticmethod
    def _top_n_ids(results: list[dict], n: int) -> set[str]:
        """取召回结果前 n 条的 memory_id 集合（保序截断后去重）。"""
        ids: list[str] = []
        for r in results:
            mid = r.get("memory_id")
            if mid and mid not in ids:
                ids.append(mid)
            if len(ids) >= n:
                break
        return set(ids)

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        """Jaccard 相似度 = |交集| / |并集|。并集为空定义为 0.0。

        为什么空并集取 0.0 而不是 1.0：
        两路都没召回到任何东西，说明这条 query 对融合毫无贡献，
        记成「完全重合」会把均值虚假拉高，正好掩盖我们要诊断的问题。
        """
        union = a | b
        if not union:
            return 0.0
        return len(a & b) / len(union)

    @classmethod
    def _recall_diagnostics(cls, cache: dict) -> dict:
        """从召回缓存统计根因数据（解释「为什么 rrf_k 无区分度」）。

        inconclusive 结论必须附带这些数字，否则读者无法判断是
        「参数真无效」还是「召回太窄导致评测无分辨力」。

        ★ `route_overlap_jaccard`（P0-4）：逐 query 算 BM25 top-N 集合与
        向量 top-N 集合的 `|∩|/|∪|`，再对全部 query 取均值。
        它把「两路同源（Jaccard 高）」与「两路解耦但评测集无分辨力（Jaccard 低）」
        这两种完全不同的根因区分开——只看交集绝对条数是分不出来的
        （交集 1 条，可能是 1/1=1.0 的完全同源，也可能是 1/25=0.04 的偶然撞车）。

        同时给出三个口径，避免「N 取多少」变成解释争议：
        - `route_overlap_jaccard`：N = RECALL_LIMIT（召回全量，主口径）
        - `route_overlap_jaccard_top10`：N = 10（与 NDCG@10 同截断位）
        - `route_overlap_jaccard_dual`：仅统计两路均非空的 query（可比基线）
        """
        from eval.rrf import RECALL_LIMIT

        if not cache:
            return {
                "cached_queries": 0,
                "route_overlap_jaccard": 0.0,
                "route_overlap_jaccard_top10": 0.0,
                "route_overlap_jaccard_dual": 0.0,
                "route_overlap_top_n": RECALL_LIMIT,
                "jaccard_dual_query_count": 0,
            }

        bm25_counts: list[int] = []
        vec_counts: list[int] = []
        overlaps: list[int] = []
        jaccards: list[float] = []
        jaccards_top10: list[float] = []
        jaccards_dual: list[float] = []
        empty_bm25 = 0
        dual_route_queries = 0

        for bm25_results, vec_results in cache.values():
            bm25_ids = cls._top_n_ids(bm25_results, RECALL_LIMIT)
            vec_ids = cls._top_n_ids(vec_results, RECALL_LIMIT)
            bm25_counts.append(len(bm25_ids))
            vec_counts.append(len(vec_ids))
            overlaps.append(len(bm25_ids & vec_ids))

            jac = cls._jaccard(bm25_ids, vec_ids)
            jaccards.append(jac)
            jaccards_top10.append(
                cls._jaccard(
                    cls._top_n_ids(bm25_results, 10),
                    cls._top_n_ids(vec_results, 10),
                )
            )

            if not bm25_ids:
                empty_bm25 += 1
            if bm25_ids and vec_ids:
                dual_route_queries += 1
                jaccards_dual.append(jac)

        def _avg(xs: list) -> float:
            return round(sum(xs) / len(xs), 4) if xs else 0.0

        def _median(xs: list[int]) -> float:
            if not xs:
                return 0.0
            s = sorted(xs)
            mid = len(s) // 2
            return float(s[mid]) if len(s) % 2 else round((s[mid - 1] + s[mid]) / 2, 4)

        return {
            "cached_queries": len(cache),
            "bm25_avg_recall": _avg(bm25_counts),
            "bm25_median_recall": _median(bm25_counts),
            "bm25_max_recall": max(bm25_counts) if bm25_counts else 0,
            "vector_avg_recall": _avg(vec_counts),
            "route_overlap_avg": _avg(overlaps),
            "route_overlap_jaccard": _avg(jaccards),
            "route_overlap_jaccard_top10": _avg(jaccards_top10),
            "route_overlap_jaccard_dual": _avg(jaccards_dual),
            "route_overlap_top_n": RECALL_LIMIT,
            "jaccard_dual_query_count": len(jaccards_dual),
            "queries_with_empty_bm25": empty_bm25,
            "dual_route_queries": dual_route_queries,
        }

    def _open_embed_cache(self):
        """按需打开 embedding 磁盘缓存并安装到 `sgme.search.vector`。

        返回 `(cache, prev_hook)`；未启用时返回 `(None, None)`。

        只在**确实会发起嵌入请求**时才打开（`rrf_skip_vector=False` 且有 cfg），
        否则纯 BM25 的单元测试会平白在 `eval/fixtures/` 下创建空库文件。
        """
        if self.rrf_embed_cache_path is None:
            return None, None
        if self.rrf_skip_vector or not self.cfg:
            return None, None

        from eval.embed_cache import EmbedCache
        from sgme.data.search import vector as sgme_vector

        try:
            cache = EmbedCache(self.rrf_embed_cache_path)
        except Exception as e:
            logger.warning("embedding 缓存打开失败，本次 run 直连端点: %s", e)
            return None, None

        prev = sgme_vector.set_embed_cache(cache)
        logger.info(
            "embedding 缓存已启用: %s（现有 %d 条，模型 %s）",
            cache.path, cache.row_count(), cache.models() or "—",
        )
        return cache, prev

    def _run_rrf(self, cases: list[EvalCase]) -> RRFMetrics:
        """执行 RRF 网格搜索阶段（建语料 → 注入 query_fn → 预热 → 网格搜索 → 诊断）。

        全程包在 embedding 缓存的安装/卸载之间：语料侧与 query 侧的 embed
        调用都会走缓存，命中即零网络。这是「两次 run 逐字段相等」的前提——
        没有缓存时，任何一次请求超时都会改变向量覆盖率，进而整段改写 NDCG。

        ★ jaccard 数据流（方案 C）：`grid.search` 前先做**预热 pass**——用
        `query_fn` 遍历 query 填满 `recall_cache`（`query_fn` 带 `recall_cache`
        属性），再复用 `_recall_diagnostics` 算出 `route_overlap_jaccard` 并入
        meta。`_diagnose` 不再静默读 0.0 兜底，缺失即抛 ValueError（不许把
        「未预热」伪装成「低重叠」）。预热与 search 共用同一 `recall_cache`，
        第二遍直接命中缓存，零额外召回。
        """
        from eval import retrieval_gt
        from eval.rrf import RRFGridSearch
        from sgme.data.search import vector as sgme_vector

        cache, prev_hook = self._open_embed_cache()
        try:
            corpus = self._setup_retrieval_corpus(cases)
            ground_truth = retrieval_gt.to_ground_truth(corpus.queries)
            query_fn = self._make_query_fn(corpus)

            # ★ 预热 pass：网格搜索前遍历 query 填满 recall_cache
            for query_text in ground_truth:
                query_fn(query_text, {"rrf_k": 60})

            # 预热后缓存已满 → 先算 route_overlap_jaccard，随 meta 一并交给 _diagnose
            prewarm_diag = self._recall_diagnostics(
                getattr(query_fn, "recall_cache", {})
            )
            grid = RRFGridSearch()
            metrics = grid.search(
                query_fn,
                ground_truth,
                k=10,
                extra_ks=(5,),
                meta={
                    "gt_mode": corpus.gt_mode,
                    "vector_available": corpus.vector_available,
                    "vector_count": corpus.vector_count,
                    "vector_coverage": corpus.vector_coverage,
                    "banner_reason": corpus.banner_reason,
                    "corpus_size": corpus.size,
                    "route_overlap_jaccard": float(
                        prewarm_diag.get("route_overlap_jaccard", 0.0)
                    ),
                },
            )

            # 预热 + search 共用同一缓存，事后完整统计（含 vector_failed_at）回填
            diagnostics = self._recall_diagnostics(
                getattr(query_fn, "recall_cache", {})
            )
            if corpus.vector_failed_at is not None:
                diagnostics["vector_failed_at"] = corpus.vector_failed_at
            metrics.recall_diagnostics = diagnostics
            metrics.route_overlap_jaccard = float(
                diagnostics.get("route_overlap_jaccard", 0.0)
            )
        finally:
            if cache is not None:
                sgme_vector.set_embed_cache(prev_hook)

        if cache is not None:
            metrics.embed_cache = cache.stats_dict()
            cache.close()
        return metrics

    # ── L1 提取 ──

    def _run_l1(
        self,
        case: EvalCase,
        dry_run: bool = False,
    ) -> tuple[list[dict], dict]:
        """执行 L1 提取。

        dry_run 模式：生成 mock 记忆（基于 ground truth 做小幅扰动）。
        正常模式：调用 sgme.engine.l1.extract_l1()。

        返回 (predictions, prompt_meta)。
        predictions 每条含: content, dimension_ids, memory_type, priority, time_velocity
        """
        if dry_run:
            return self._mock_l1(case)

        # 正常模式：调用真实提炼引擎
        try:
            from sgme import config as sgme_config
            from sgme.engine import l1 as l1_engine
            from sgme.engine import normalize
            from sgme.prompts import BucketCtx
            from sgme.data import memory_dao

            dimensions = self.cfg.get("dimensions", [])
            llm_cfg = self.cfg.get("llm", {})

            bucket_ctx = None
            if self.prompt_version:
                bucket_ctx = BucketCtx(
                    bucket_key=case.case_id,
                    overrides={
                        "l1_extraction": self.prompt_version,
                    },
                )

            raw_memories, provider, prompt_meta = l1_engine.extract_l1(
                case.conversation,
                dimensions,
                llm_cfg,
                bucket_ctx=bucket_ctx,
            )

            # 归一化维度
            if self.mem_conn:
                alias_map = memory_dao.build_alias_map(self.mem_conn)
                registry_names = {d["id"]: d.get("display_name", d["id"])
                                  for d in dimensions}
                for mem in raw_memories:
                    dims = mem.get("dimensions", [])
                    if dims:
                        norm_ids, _stats = normalize.normalize_batch(
                            dims, alias_map, registry_names,
                        )
                        mem["dimension_ids"] = norm_ids

            return raw_memories, prompt_meta

        except ImportError as e:
            logger.warning("sgme 引擎不可用，回退到 mock 模式: %s", e)
            return self._mock_l1(case)
        except Exception as e:
            logger.error("L1 提取失败: %s", e)
            raise

    def _mock_l1(self, case: EvalCase) -> tuple[list[dict], dict]:
        """生成 mock L1 输出（基于 ground truth 做小幅扰动，模拟真实 LLM 行为）。

        扰动策略（制造可度量的误差）：
        - easy 用例：直接返回 ground truth（完美预测）
        - medium 用例：随机丢弃 1 个维度
        - hard 用例：随机丢弃 1 个维度 + 调整 priority ±10

        这确保 dry-run 模式下度量管线可自验证。
        """
        import random
        # 固定 seed 保证可复现
        random.seed(hash(case.case_id) % (2**31))

        mock_memories: list[dict] = []
        for gt_mem in case.expected_l1.memories:
            dims = list(gt_mem.dimensions)

            if case.difficulty == "medium" and len(dims) > 1:
                # 丢弃 1 个维度
                dims.pop(random.randint(0, len(dims) - 1))
            elif case.difficulty == "hard":
                if len(dims) > 1:
                    dims.pop(random.randint(0, len(dims) - 1))
                priority = max(0, min(100, gt_mem.priority + random.randint(-10, 10)))
            else:
                priority = gt_mem.priority

            mock_memories.append({
                "content": gt_mem.content,
                "dimension_ids": dims,
                "dimensions": dims,
                "memory_type": gt_mem.memory_type,
                "priority": priority if case.difficulty == "hard" else gt_mem.priority,
                "time_velocity": gt_mem.time_velocity,
                "source_message_ids": list(gt_mem.source_message_ids),
            })

        meta = {
            "stage": "l1_extraction",
            "version": "mock",
            "variant": None,
        }
        logger.debug("mock L1: case=%s diff=%s memories=%d",
                     case.case_id, case.difficulty, len(mock_memories))
        return mock_memories, meta

    # ── P0 目标检查 ──

    @staticmethod
    def _check_p0_targets(l1: L1Metrics, l2: L2Metrics) -> dict[str, str]:
        """检查 P0 指标是否达标（PRD §7.1）。

        返回 {指标名: "green"|"yellow"|"red"}。
        """
        status: dict[str, str] = {}

        # L1 维度微平均 F1 ≥ 0.75
        if l1.dimension_micro_f1 >= 0.75:
            status["L1 F1"] = "green"
        elif l1.dimension_micro_f1 >= 0.60:
            status["L1 F1"] = "yellow"
        else:
            status["L1 F1"] = "red"

        # L1 Strict Match Rate ≥ 0.50
        if l1.strict_match_rate >= 0.50:
            status["Strict Match"] = "green"
        elif l1.strict_match_rate >= 0.35:
            status["Strict Match"] = "yellow"
        else:
            status["Strict Match"] = "red"

        # memory_type Accuracy ≥ 0.85
        if l1.memory_type_accuracy >= 0.85:
            status["memory_type Acc"] = "green"
        elif l1.memory_type_accuracy >= 0.70:
            status["memory_type Acc"] = "yellow"
        else:
            status["memory_type Acc"] = "red"

        # time_velocity Accuracy ≥ 0.80
        if l1.time_velocity_accuracy >= 0.80:
            status["time_velocity Acc"] = "green"
        elif l1.time_velocity_accuracy >= 0.65:
            status["time_velocity Acc"] = "yellow"
        else:
            status["time_velocity Acc"] = "red"

        # L2 Section 命中率 ≥ 0.70
        if l2.section_hit_rate >= 0.70:
            status["Section Hit Rate"] = "green"
        elif l2.section_hit_rate >= 0.50:
            status["Section Hit Rate"] = "yellow"
        else:
            status["Section Hit Rate"] = "red"

        # 画像质量 ≥ 0.50
        if l2.profile_quality >= 0.50:
            status["Profile Quality"] = "green"
        elif l2.profile_quality >= 0.35:
            status["Profile Quality"] = "yellow"
        else:
            status["Profile Quality"] = "red"

        return status
