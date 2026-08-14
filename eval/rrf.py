"""eval/rrf.py：RRF 网格搜索 + NDCG 计算 + 区分度诊断。

- `RRFGridSearch.search`：对 `param_space` 笛卡尔积逐组评测，产出 `RRFMetrics`
- `RRFGridSearch._diagnose`：★ 诚实区分度诊断（不伪造 best_k）
- `_compute_ndcg`：独立的 NDCG@k 计算（可独立测试）

设计依据：docs/design/SGME-评测框架设计-v0.1.md §1.5。

★★ 最重要的约定：诚实诊断 ★★
架构师在当前评测集上实证：`rrf_k` 对 NDCG 的极差 < 1e-6，即**零区分度**。
根因是 FTS5 `unicode61` tokenizer 对中文按标点整段切 token，召回极窄
（BM25 中位仅 1 条），RRF 融合前后排序几乎不变。
因此本模块**绝不**用 tie-break 结果冒充「最优 k」：
结论按 PRD §6.4.3 五值枚举（conclusive / inconclusive_no_effect /
inconclusive_below_noise / inconclusive_bm25_only / no_queries），
除 `conclusive` 外 `recommended_k` 恒为 None。
`best_params` 此时只是形式上的确定性 tie-break 产物，报告层必须显式标注不可采信。
"""

from __future__ import annotations

import logging
import math
from itertools import product
from typing import Any, Callable, Iterable, Sequence

from eval.models import RRFMetrics

logger = logging.getLogger("eval.rrf")

# 单路召回条数上限（query_fn 返回列表长度上界）。
# 20 > NDCG@10 所需的 10，留出 RRF 融合的重排空间，又不至于让 LIKE 兜底扫全表。
RECALL_LIMIT = 20

# 区分度判定阈值。`_compute_ndcg` 已 round 到 4 位小数，
# 因此任何 < 1e-6 的极差都只能是「完全相同」的浮点噪声。
EPS = 1e-6

# 五值 conclusion 阈值（PRD §6.4.3，诚实红线）：
#   NDCG_SIG：ndcg_spread ≥ 0.01 才算「真实最优 k」（conclusive）
#   NDCG_TIE：ndcg_spread < 1e-9 视为「完全无作用点」（inconclusive_no_effect）
#   J_LOW   ：route_overlap_jaccard < 0.20 视为「低重叠」（reporter 归因用）
NDCG_SIG = 0.01
NDCG_TIE = 1e-9
J_LOW = 0.20

# RRF 参数默认搜索空间。
#
# 仅保留 `rrf_k`——其余参数在当前引擎（sgme/search/）下**不可调**或**无效**，
# 留在空间里只会产出成倍的重复 NDCG 行，制造「跑了很多组合」的假象：
#   - bm25_k1 / bm25_b：SQLite FTS5 的 bm25() 内置排序函数不暴露 k1/b 调参入口
#   - bm25_weight：`sgme.search.rrf.rrf_merge` 是标准无权 RRF（两路等权），无权重入参
#   - top_k：≥10 时对 NDCG@10 完全无影响（只影响截断长度），RECALL_LIMIT 已固定为 20
DEFAULT_PARAM_SPACE: dict[str, list] = {
    "rrf_k": [10, 30, 60, 90, 120],
}


def _mean(values: Sequence[float]) -> float:
    """算术平均（空序列返回 0.0）。"""
    if not values:
        return 0.0
    return sum(values) / len(values)


class RRFGridSearch:
    """RRF 参数网格搜索。

    `search()` 通过注入的 `query_fn` 对接真实检索（v0.4 T13 已实现 sgme/search/）。
    `_compute_ndcg()` 可独立用于任何排序列表的 NDCG 计算。
    """

    def __init__(self, param_space: dict[str, list] | None = None):
        """初始化网格搜索。

        param_space: 参数空间字典，默认使用 DEFAULT_PARAM_SPACE。
        """
        self.param_space = param_space or dict(DEFAULT_PARAM_SPACE)
        self._results: list[dict] = []
        self._best: dict | None = None

    def search(
        self,
        query_fn: Callable[[str, dict], list[str]],
        ground_truth: dict[str, list[str]],
        k: int = 10,
        extra_ks: tuple[int, ...] = (5,),
        meta: dict | None = None,
    ) -> RRFMetrics:
        """网格搜索最优 RRF 参数 + 区分度诊断。

        参数：
          query_fn(query_text, params) → 排序后的 memory_id 列表。
            注入契约（调用方保证）：
              1. `params` 至少含 `rrf_k: int`
              2. 返回值去重、按相关性降序、长度 ≤ RECALL_LIMIT
              3. 纯函数：同 query + 同 params ⇒ 同结果（否则可复现性验收失败）
              4. 不抛异常（本方法仍做 try 兜底，异常 → 空列表 + 告警）
          ground_truth: {query_text: [relevant_memory_ids]}，相关集必须非空
          k: 主评测截断位（默认 10）
          extra_ks: 附加截断位（默认 (5,)）
          meta: 上下文透传进 RRFMetrics，可含
            `gt_mode` / `vector_available` / `vector_count` / `vector_coverage` /
            `banner_reason` / `corpus_size` / `route_overlap_jaccard` / `embed_cache`

        返回 `RRFMetrics`。★ `ndcg_spread < EPS` 时 `recommended_k=None`，
        绝不用 tie-break 结果冒充推荐值。
        """
        meta = dict(meta or {})
        queries: list[str] = list(ground_truth.keys())
        param_names: list[str] = list(self.param_space.keys())
        value_lists: list[list] = [list(self.param_space[name]) for name in param_names]

        self._results = []
        self._best = None

        # query → 各参数组合下的 top-k 排序列表（用于 rank_sensitive_ratio）
        rankings: dict[str, list[tuple[str, ...]]] = {q: [] for q in queries}

        combos: Iterable[tuple] = product(*value_lists) if param_names else [()]
        for combo in combos:
            params: dict[str, Any] = dict(zip(param_names, combo))
            main_scores: list[float] = []
            extra_scores: dict[int, list[float]] = {ek: [] for ek in extra_ks}

            for query_text in queries:
                relevant = list(ground_truth.get(query_text) or [])
                try:
                    predicted = query_fn(query_text, dict(params))
                except Exception as e:  # query_fn 应自兜底，此处二次防线
                    logger.warning(
                        "query_fn 异常（params=%s query=%.40s…）: %s",
                        params, query_text, e,
                    )
                    predicted = []
                predicted = [str(p) for p in (predicted or [])]

                rankings[query_text].append(tuple(predicted[:k]))
                main_scores.append(self._compute_ndcg(predicted, relevant, k))
                for ek in extra_ks:
                    extra_scores[ek].append(self._compute_ndcg(predicted, relevant, ek))

            entry: dict[str, Any] = dict(params)
            entry["ndcg10"] = round(_mean(main_scores), 4)   # 主 k（= 入参 k，默认 10）
            for ek in extra_ks:
                entry[f"ndcg{ek}"] = round(_mean(extra_scores[ek]), 4)
            entry["ndcg_k"] = k
            entry["query_count"] = len(queries)
            self._results.append(entry)

        # tie-break：NDCG 相同则取更小的 rrf_k（确定性，与字典/浮点顺序无关）
        if self._results:
            self._best = max(
                self._results,
                key=lambda r: (r["ndcg10"], -int(r.get("rrf_k", 0))),
            )

        diag = self._diagnose(queries, rankings, meta)

        metrics = RRFMetrics(
            best_ndcg10=float(self._best["ndcg10"]) if self._best else 0.0,
            best_params=(
                {name: self._best[name] for name in param_names}
                if self._best else {}
            ),
            all_results=list(self._results),
            param_sensitivity=self._param_sensitivity(),
            ndcg_k=k,
            best_ndcg5=(
                float(self._best.get(f"ndcg{extra_ks[0]}", 0.0))
                if (self._best and extra_ks) else 0.0
            ),
            gt_mode=str(meta.get("gt_mode", "message")),
            vector_available=bool(meta.get("vector_available", False)),
            vector_count=int(meta.get("vector_count", 0)),
            vector_coverage=float(meta.get("vector_coverage", 0.0)),
            banner_reason=str(meta.get("banner_reason", "") or ""),
            query_count=len(queries),
            corpus_size=int(meta.get("corpus_size", 0)),
            ndcg_spread=diag["ndcg_spread"],
            discriminative=diag["discriminative"],
            rank_sensitive_ratio=diag["rank_sensitive_ratio"],
            route_overlap_jaccard=float(meta.get("route_overlap_jaccard", 0.0)),
            conclusion=diag["conclusion"],
            recommended_k=diag["recommended_k"],
            embed_cache=dict(meta.get("embed_cache") or {}),
        )

        logger.info(
            "RRF 网格搜索完成: 组合=%d 查询=%d best_ndcg@%d=%.4f "
            "spread=%.6f discriminative=%s conclusion=%s recommended_k=%s "
            "vector=%d/%d(available=%s) jaccard=%.4f",
            len(self._results), len(queries), k, metrics.best_ndcg10,
            metrics.ndcg_spread, metrics.discriminative,
            metrics.conclusion, metrics.recommended_k,
            metrics.vector_count, metrics.corpus_size,
            metrics.vector_available, metrics.route_overlap_jaccard,
        )
        return metrics

    # ── 诊断 ──

    def _diagnose(
        self,
        queries: list[str],
        rankings: dict[str, list[tuple[str, ...]]],
        meta: dict | None = None,
    ) -> dict[str, Any]:
        """★ 诚实区分度诊断（PRD §6.4.3 五值 conclusion）。

        - `ndcg_spread` = 各参数组合 NDCG 的 max - min
        - `discriminative` = `ndcg_spread >= EPS`（布尔字段保留，旧口径，仅过程诊断）
        - `rank_sensitive_ratio` = 「top-k 排序列表随参数变化」的查询占比
          （NDCG 可能因相关文档恰好留在同一档而掩盖排序变化，故单独统计）
        - `conclusion` 五值枚举；`recommended_k` 仅 `conclusive` 时非 None

        meta 提供 `vector_available` 与 `route_overlap_jaccard`。
        jaccard 用于 reporter 归因分流；spread≈0 时结论一律收敛到
        `inconclusive_no_effect`（k 无作用点，与重叠度无关——高重叠同源同理）。

        ★ jaccard 数据流缺口修复（方案 C）：仅当向量可用、结论需要按重叠度
        归因时才读 `route_overlap_jaccard`，缺失即抛 ValueError（不许静默当 0）。
        调用方（`eval/runner.py::_run_rrf`）必须在 `grid.search` 前预热
        `recall_cache` 并把 jaccard 并入 meta。无查询 / 单路 BM25 场景不读
        jaccard（结论不依赖它，缺失不影响判定）。
        """
        meta = dict(meta or {})
        ndcgs = [r["ndcg10"] for r in self._results]
        spread = round(max(ndcgs) - min(ndcgs), 6) if ndcgs else 0.0
        discriminative = bool(ndcgs) and spread >= EPS

        sensitive = 0
        for query_text in queries:
            variants = {tuple(x) for x in rankings.get(query_text, [])}
            if len(variants) > 1:
                sensitive += 1
        rank_sensitive_ratio = round(sensitive / len(queries), 4) if queries else 0.0

        vector_available = bool(meta.get("vector_available", False))

        if not queries:
            conclusion = "no_queries"
        elif not vector_available:
            conclusion = "inconclusive_bm25_only"
        else:
            # ★ jaccard 数据流缺口修复（方案 C）：只有向量可用、结论需要按重叠度
            # 归因时才读 jaccard。缺失即抛 ValueError（不许静默当 0）——调用方必须
            # 先在 grid.search 前预热 recall_cache 并把 route_overlap_jaccard 并入
            # meta（见 eval/runner.py::_run_rrf）。
            jaccard_val = meta.get("route_overlap_jaccard")
            if jaccard_val is None:
                raise ValueError(
                    "meta 缺 route_overlap_jaccard：调用方未在 grid.search 前预热 "
                    "recall_cache 并回填 jaccard（eval/runner.py::_run_rrf 方案 C）"
                )
            jaccard = float(jaccard_val)

            if spread >= NDCG_SIG:
                conclusion = "conclusive"
            elif spread < NDCG_TIE and jaccard < J_LOW:
                conclusion = "inconclusive_no_effect"
            elif 0.0 < spread < NDCG_SIG:
                conclusion = "inconclusive_below_noise"
            else:
                # 兜底：vector=true 且 spread < NDCG_TIE 但 jaccard ≥ J_LOW
                # （高重叠同源、两路排序一致）——PRD 表格未显式覆盖；k 仍无作用点，
                # 归入 no_effect，归因由 reporter._jaccard_verdict 按 jaccard 分流。
                conclusion = "inconclusive_no_effect"

        recommended_k: int | None = None
        if conclusion == "conclusive" and self._best is not None and "rrf_k" in self._best:
            recommended_k = int(self._best["rrf_k"])

        return {
            "ndcg_spread": spread,
            "discriminative": discriminative,
            "rank_sensitive_ratio": rank_sensitive_ratio,
            "conclusion": conclusion,
            "recommended_k": recommended_k,
        }

    def _param_sensitivity(self) -> dict:
        """逐参数敏感度：{param: {mean_ndcg_by_value: {值: 均值}, spread: 极差}}。"""
        sensitivity: dict[str, Any] = {}
        for name in self.param_space:
            groups: dict[Any, list[float]] = {}
            for r in self._results:
                if name not in r:
                    continue
                groups.setdefault(r[name], []).append(r["ndcg10"])
            if not groups:
                continue
            means = {str(v): round(_mean(scores), 4) for v, scores in groups.items()}
            spread = round(max(means.values()) - min(means.values()), 6)
            sensitivity[name] = {"mean_ndcg_by_value": means, "spread": spread}
        return sensitivity

    # ── NDCG ──

    @staticmethod
    def _compute_ndcg(
        predicted: list[str],
        relevant: list[str],
        k: int = 10,
    ) -> float:
        """计算 NDCG@k（Normalized Discounted Cumulative Gain）。

        predicted: 预测排序列表（按相关性降序）
        relevant: ground truth 相关文档 id 列表（顺序不重要，用集合判等）
        k: 截断位置

        公式：
        DCG@k = Σ_{i=1}^{k} rel_i / log₂(i+1)
        IDCG@k = 理想排序下的 DCG@k
        NDCG@k = DCG@k / IDCG@k

        rel_i = 1（第 i 位在相关列表中）/ 0（不在）

        注：空相关集返回 1.0（任何排序都最优）。GT 构造必须保证 relevant 非空，
        否则均值会被 1.0 拉高，产出虚假的高 NDCG。
        """
        if k <= 0:
            return 0.0

        relevant_set = set(relevant)
        if not relevant_set:
            return 1.0  # 无相关文档 → 任何排序都是最优的

        # DCG@k
        dcg = 0.0
        for i in range(min(k, len(predicted))):
            if predicted[i] in relevant_set:
                dcg += 1.0 / math.log2(i + 2)  # i+2 因为 i 从 0 开始

        # IDCG@k：理想情况下所有相关文档排在最前面
        idcg = 0.0
        ideal_n = min(k, len(relevant))
        for i in range(ideal_n):
            idcg += 1.0 / math.log2(i + 2)

        if idcg == 0.0:
            return 0.0

        return round(dcg / idcg, 4)

    @staticmethod
    def compute_ndcg(predicted: list[str], relevant: list[str], k: int = 10) -> float:
        """公开别名（等同于 _compute_ndcg）。"""
        return RRFGridSearch._compute_ndcg(predicted, relevant, k)

    def best_params(self) -> dict | None:
        """返回当前搜索结果中的最优参数组合。

        ⚠️ 无区分度（`RRFMetrics.discriminative=False`）时，本返回值只是
        确定性 tie-break 产物，**不构成参数推荐**。
        """
        return self._best

    def status(self) -> str:
        """返回当前网格搜索状态。"""
        if self._results:
            best = self._best["ndcg10"] if self._best else "N/A"
            return f"completed: {len(self._results)} combinations, best NDCG@10={best}"
        return "not_started: 尚未执行网格搜索（调用 search(query_fn, ground_truth) 启动）"


def compute_ndcg(predicted: list[str], relevant: list[str], k: int = 10) -> float:
    """独立 NDCG@k 函数（便捷入口）。"""
    return RRFGridSearch._compute_ndcg(predicted, relevant, k)
