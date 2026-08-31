"""eval/reporter.py：报告生成器。

- generate_report_json: 输出 report.json（结构化指标 + 用例级明细）
- generate_report_md: Markdown 基线报告（含目标对比表）

设计依据：docs/design/SGME-评测框架设计-v0.1.md §1.1、PRD §7.2。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.models import EvalResult, InjectMetrics, L1Metrics, L2Metrics, RRFMetrics

logger = logging.getLogger("eval.reporter")

# P0 基线目标（PRD §7.1）
# ⚠️ RRF 检索指标**不进** P0 准入门槛：RRF 是调参诊断，不是提炼质量门槛，
#    掺进来会让 passed_p0 的语义漂移。本字典与 EvalRunner._check_p0_targets 保持一字不改。
P0_TARGETS = {
    "L1 F1": 0.75,
    "Strict Match": 0.50,
    "memory_type Acc": 0.85,
    "time_velocity Acc": 0.80,
    "Section Hit Rate": 0.70,
    "Profile Quality": 0.50,
}


def generate_report_json(result: EvalResult, output_dir: Path) -> Path:
    """生成 report.json。

    输出路径：output_dir/report.json
    返回生成的文件路径。
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    data = _build_json_report(result)
    report_path = output_dir / "report.json"

    with report_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    logger.info("report.json 已生成: %s", report_path)
    return report_path


def _build_json_report(result: EvalResult) -> dict:
    """构建 report.json 数据结构。"""
    l1 = result.l1
    l2 = result.l2

    report: dict[str, Any] = {
        "run_id": result.run_id,
        "timestamp": result.timestamp,
        "prompt_versions": result.prompt_versions,
        "summary": {
            "total_cases": result.summary.total_cases,
            "passed_p0": result.summary.passed_p0,
            "p0_status": result.summary.p0_status,
            "duration_seconds": result.summary.duration_seconds,
        },
    }

    if l1:
        per_dim = {}
        for dim_id, df1 in l1.per_dimension_f1.items():
            per_dim[dim_id] = {
                "f1": df1.f1,
                "precision": df1.precision,
                "recall": df1.recall,
                "tp": df1.tp,
                "fp": df1.fp,
                "fn": df1.fn,
            }

        report["l1"] = {
            "dimension_micro_f1": l1.dimension_micro_f1,
            "dimension_micro_precision": l1.dimension_micro_precision,
            "dimension_micro_recall": l1.dimension_micro_recall,
            "per_dimension_f1": per_dim,
            "strict_match_rate": l1.strict_match_rate,
            "memory_type_accuracy": l1.memory_type_accuracy,
            "time_velocity_accuracy": l1.time_velocity_accuracy,
            "priority_mae": l1.priority_mae,
            "total_tp": l1.total_tp,
            "total_fp": l1.total_fp,
            "total_fn": l1.total_fn,
        }

    if l2:
        report["l2"] = {
            "section_hit_rate": l2.section_hit_rate,
            "section_misentry_rate": l2.section_misentry_rate,
            "section_miss_rate": l2.section_miss_rate,
            "profile_quality": l2.profile_quality,
            "total_evaluated": l2.total_evaluated,
        }

    # RRF 检索调参段（全字段；两次同输入运行应逐字段相等）
    rrf = result.rrf
    if rrf:
        report["rrf"] = {
            "best_ndcg10": rrf.best_ndcg10,
            "best_ndcg5": rrf.best_ndcg5,
            "best_params": rrf.best_params,
            "all_results": rrf.all_results,
            "param_sensitivity": rrf.param_sensitivity,
            "ndcg_k": rrf.ndcg_k,
            "gt_mode": rrf.gt_mode,
            "vector_available": rrf.vector_available,
            "vector_count": rrf.vector_count,
            "vector_coverage": rrf.vector_coverage,
            "banner_reason": rrf.banner_reason,
            "query_count": rrf.query_count,
            "corpus_size": rrf.corpus_size,
            "ndcg_spread": rrf.ndcg_spread,
            "discriminative": rrf.discriminative,
            "rank_sensitive_ratio": rrf.rank_sensitive_ratio,
            "route_overlap_jaccard": rrf.route_overlap_jaccard,
            "conclusion": rrf.conclusion,
            "recommended_k": rrf.recommended_k,
            "recall_at_k": rrf.recall_at_k.as_dict() if rrf.recall_at_k else None,
            "recall_diagnostics": rrf.recall_diagnostics,
            "embed_cache": rrf.embed_cache,
        }

    # 注入效果段（T-20，PRD §5.4）
    inject = result.inject
    if inject is not None:
        report["inject"] = {
            "inject_hit_rate": inject.inject_hit_rate,
            "reference_coverage": inject.reference_coverage,
            "total_blocks": inject.total_blocks,
            "relevant_blocks": inject.relevant_blocks,
            "total_referenced": inject.total_referenced,
            "hit_and_referenced": inject.hit_and_referenced,
        }

    # per_case 明细
    report["per_case"] = []
    for cr in result.per_case:
        entry = {
            "case_id": cr.case_id,
            "difficulty": cr.difficulty,
            "l1_f1": cr.l1_f1,
            "strict_match": cr.strict_match,
            "matched_memories": cr.matched_memories,
            "unmatched_pred": cr.unmatched_pred,
            "unmatched_gt": cr.unmatched_gt,
            "dimension_details": cr.dimension_details,
            "inject_hit_rate": cr.inject_hit_rate,
            "inject_reference_coverage": cr.inject_reference_coverage,
            "error": cr.error,
        }
        report["per_case"].append(entry)

    return report


def generate_report_md(result: EvalResult, output_dir: Path) -> Path:
    """生成 Markdown 基线报告。

    输出路径：output_dir/report.md
    包含：总览面板（目标对比表）+ 维度 F1 表 + 逐用例明细。
    返回生成的文件路径。
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    lines = _build_markdown_report(result)
    report_path = output_dir / "report.md"

    with report_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")

    logger.info("report.md 已生成: %s", report_path)
    return report_path


def _build_markdown_report(result: EvalResult) -> list[str]:
    """构建 Markdown 报告内容。"""
    lines: list[str] = []
    lines.append("# SGME 评测基线报告")
    lines.append("")
    lines.append(f"- **Run ID**: `{result.run_id}`")
    lines.append(f"- **时间戳**: {result.timestamp}")
    lines.append(f"- **提示词版本**: {result.prompt_versions}")
    lines.append(f"- **用例总数**: {result.summary.total_cases}")
    lines.append(f"- **耗时**: {result.summary.duration_seconds}s")
    lines.append(f"- **P0 全通过**: {'✅ 是' if result.summary.passed_p0 else '❌ 否'}")
    lines.append("")

    l1 = result.l1
    l2 = result.l2

    # ── 总览面板 ──
    lines.append("## 📊 总览面板")
    lines.append("")
    lines.append("| 指标 | 当前值 | 目标值 | 状态 |")
    lines.append("|------|--------|--------|------|")

    if l1:
        overview_rows = [
            ("L1 维度微平均 F1", l1.dimension_micro_f1, P0_TARGETS["L1 F1"]),
            ("Strict Match Rate", l1.strict_match_rate, P0_TARGETS["Strict Match"]),
            ("memory_type Accuracy", l1.memory_type_accuracy, P0_TARGETS["memory_type Acc"]),
            ("time_velocity Accuracy", l1.time_velocity_accuracy, P0_TARGETS["time_velocity Acc"]),
        ]
        for label, val, target in overview_rows:
            status = _status_emoji(val, target)
            lines.append(f"| {label} | {val:.4f} | {target} | {status} |")

    if l2:
        overview_rows_l2 = [
            ("L2 Section 命中率", l2.section_hit_rate, P0_TARGETS["Section Hit Rate"]),
            ("画像质量综合分", l2.profile_quality, P0_TARGETS["Profile Quality"]),
        ]
        for label, val, target in overview_rows_l2:
            status = _status_emoji(val, target)
            lines.append(f"| {label} | {val:.4f} | {target} | {status} |")

    lines.append("")

    # ── L1 详细指标 ──
    if l1:
        lines.append("## L1 维度标注指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|------|----|")
        lines.append(f"| 微平均 Precision | {l1.dimension_micro_precision:.4f} |")
        lines.append(f"| 微平均 Recall | {l1.dimension_micro_recall:.4f} |")
        lines.append(f"| 微平均 F1 | {l1.dimension_micro_f1:.4f} |")
        lines.append(f"| Strict Match Rate | {l1.strict_match_rate:.4f} |")
        lines.append(f"| memory_type Accuracy | {l1.memory_type_accuracy:.4f} |")
        lines.append(f"| time_velocity Accuracy | {l1.time_velocity_accuracy:.4f} |")
        lines.append(f"| priority MAE | {l1.priority_mae:.4f} |")
        lines.append(f"| TP / FP / FN | {l1.total_tp} / {l1.total_fp} / {l1.total_fn} |")
        lines.append("")

        # 逐维度 F1
        if l1.per_dimension_f1:
            lines.append("### 逐维度 F1")
            lines.append("")
            lines.append("| 维度 ID | F1 | Precision | Recall | TP | FP | FN |")
            lines.append("|---------|----|-----------|--------|----|----|----|")
            for dim_id, df1 in sorted(l1.per_dimension_f1.items()):
                warn = " ⚠️" if df1.f1 < 0.6 else ""
                lines.append(
                    f"| {dim_id}{warn} | {df1.f1:.4f} | {df1.precision:.4f} | "
                    f"{df1.recall:.4f} | {df1.tp} | {df1.fp} | {df1.fn} |"
                )
            lines.append("")

    # ── L2 指标 ──
    if l2 and l2.total_evaluated > 0:
        lines.append("## L2 模板查询指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|------|----|")
        lines.append(f"| Section 命中率 | {l2.section_hit_rate:.4f} |")
        lines.append(f"| Section 误入率 | {l2.section_misentry_rate:.4f} |")
        lines.append(f"| Section 漏出率 | {l2.section_miss_rate:.4f} |")
        lines.append(f"| 画像质量综合分 | {l2.profile_quality:.4f} |")
        lines.append("")

    # ── 注入效果（T-20，PRD §5.4） ──
    if result.inject is not None and result.inject.total_blocks > 0:
        lines.append("## 💉 模板注入效果")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|------|----|")
        lines.append(f"| 注入命中率 | {result.inject.inject_hit_rate:.4f} |")
        lines.append(f"| 引用覆盖率 | {result.inject.reference_coverage:.4f} |")
        lines.append(f"| 注入块总数 | {result.inject.total_blocks} |")
        lines.append(f"| 相关块数 | {result.inject.relevant_blocks} |")
        lines.append(f"| 引用记忆数 | {result.inject.total_referenced} |")
        lines.append(f"| 命中且引用数 | {result.inject.hit_and_referenced} |")
        lines.append("")

    # ── RRF 检索调参 ──
    if result.rrf is not None:
        lines.extend(_build_rrf_section(result.rrf))

    # ── 逐用例明细 ──
    lines.append("## 📋 逐用例明细")
    lines.append("")
    lines.append("| Case ID | Difficulty | L1 F1 | Strict | Matched | Unm. Pred | Unm. GT | Error |")
    lines.append("|---------|------------|-------|--------|---------|-----------|---------|-------|")
    for cr in result.per_case:
        error_str = cr.error[:40] if cr.error else ""
        lines.append(
            f"| {cr.case_id} | {cr.difficulty} | {cr.l1_f1:.4f} | "
            f"{'✅' if cr.strict_match else '❌'} | {cr.matched_memories} | "
            f"{cr.unmatched_pred} | {cr.unmatched_gt} | {error_str} |"
        )
    lines.append("")

    # ── 难度分层统计 ──
    by_diff: dict[str, list] = {"easy": [], "medium": [], "hard": []}
    for cr in result.per_case:
        if cr.difficulty in by_diff:
            by_diff[cr.difficulty].append(cr.l1_f1)

    lines.append("### 难度分层 F1")
    lines.append("")
    lines.append("| 难度 | 用例数 | 平均 L1 F1 |")
    lines.append("|------|--------|-----------|")
    for diff in ["easy", "medium", "hard"]:
        vals = by_diff.get(diff, [])
        avg = sum(vals) / len(vals) if vals else 0.0
        lines.append(f"| {diff} | {len(vals)} | {avg:.4f} |")
    lines.append("")

    lines.append("---")
    lines.append(f"*报告生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}*")

    return lines


def _banner_explain(rrf: RRFMetrics) -> str:
    """把 `banner_reason` 翻译成一句人话（Markdown 引用行）。

    部分覆盖必须单独讲清楚：它比「完全没有向量」更隐蔽也更危险——
    只有一部分记忆进了 `memory_vectors`，向量路永远召不回另一部分，
    NDCG 会随「这次嵌上了多少条」上下漂移。
    """
    reason = rrf.banner_reason or ""
    if reason.startswith("vector_partial_"):
        return (
            f"> 语料只有 {rrf.vector_count}/{rrf.corpus_size} 条完成嵌入（部分覆盖）。"
            "未嵌入的记忆在向量路上**永久不可见**，两路不再可比，"
            "按 PRD 约定判定为不可用。"
        )
    if reason.startswith("vector_unavailable_"):
        return "> 首条嵌入即失败，embeddings 端点不可达，一条向量都没写入。"
    if reason == "vector_skipped_by_flag":
        return "> 命令行显式指定 `--rrf-skip-vector`，本次主动跳过向量嵌入。"
    if reason == "vector_no_cfg":
        return "> 未提供 cfg，无法解析 embeddings 端点地址。"
    if reason == "empty_corpus":
        return "> 语料为空，没有任何记忆可供嵌入。"
    return "> 本次运行 embeddings 端点不可达（或显式 `--rrf-skip-vector`）。"


def _jaccard_verdict(jaccard: float, dual_queries: int, diag: dict) -> list[str]:
    """把 `route_overlap_jaccard` 翻译成根因判定（PM 基线：同源≈0.694 / 解耦≈0.121）。

    低 Jaccard 分支必须结合 `diag["bm25_avg_recall"]` 判读（PRD §6.4.3，修正
    归因写反 bug）：
      - `bm25_avg_recall < 3` ⇒ 低重叠是**列表长度悬殊伪影**（BM25 几乎不召回、
        向量恒满 20 条撑爆分母），真相是 **BM25 退化「伪解耦」**——两路并非
        各说各话而是高度一致（p0-runA：Jaccard=0.0746、bm25_avg_recall=1.74、
        87.4% BM25 命中被向量集合包含）；
      - `bm25_avg_recall ≥ 3` ⇒ 两路确实解耦，才是「评测集缺乏分辨力」。
    """
    diag = dict(diag or {})
    lines: list[str] = []
    if dual_queries <= 0:
        lines.append("> ⚪ 没有任何 query 同时拿到两路召回，Jaccard 不具解释力"
                     "（本次融合场景实际未发生）。")
        return lines
    if jaccard >= 0.5:
        lines.append(f"> 🟠 Jaccard = **{jaccard:.4f}** ≥ 0.5，接近 PM 的「同源」基线 0.694。")
        lines.append("> 两路召回的基本是同一批记忆 ⇒ 融合没有引入新信息，")
        lines.append("> 「rrf_k 无区分度」是**两路同源造成的假象**，不是参数本身无效。")
    elif jaccard <= 0.2:
        bm25_avg = float(diag.get("bm25_avg_recall", 0.0) or 0.0)
        if bm25_avg < 3.0:
            median = float(diag.get("bm25_median_recall", 0.0) or 0.0)
            empty = int(diag.get("queries_with_empty_bm25", 0) or 0)
            total = int(diag.get("cached_queries", 0) or 0)
            overlap_avg = float(diag.get("route_overlap_avg", 0.0) or 0.0)
            ratio = (overlap_avg / bm25_avg) if bm25_avg > 0 else 0.0
            lines.append(f"> 🔴 两路低重叠系 **BM25 退化**所致（伪解耦）——")
            lines.append(f"> BM25 平均召回 {bm25_avg:.2f}（中位 {median:.0f}）、"
                         f"空召回 {empty}/{total}、")
            lines.append(f"> {ratio:.1%} 的 BM25 命中被向量集合包含 ⇒ 两路并非各说各话，")
            lines.append("> 而是 BM25 召回太窄（FTS5 `unicode61` 整段切 token），"
                         "融合缺可重排文档。")
        else:
            lines.append(f"> 🔵 Jaccard = **{jaccard:.4f}** ≤ 0.2，接近 PM 的「解耦」基线 0.121。")
            lines.append("> 两路确实各说各话、融合有信息增益，但 NDCG 仍分不出优劣 ⇒")
            lines.append("> 根因是**本评测集缺乏分辨力**（召回过窄 / GT 过少），不是两路同源。")
    else:
        lines.append(f"> 🟡 Jaccard = **{jaccard:.4f}**，落在同源基线 0.694 与解耦基线 0.121 之间。")
        lines.append("> 两路部分重合，无法单凭该指标归因，需结合下方召回量一起看。")
    return lines


def _build_rrf_section(rrf: RRFMetrics) -> list[str]:
    """构建「RRF 检索调参」章节。

    ★ 诚实呈现原则（不可弱化）：
    - `conclusion == "conclusive"` → 给出可复制的 `search.rrf.k: <best>`
    - 其余四态（no_effect / below_noise / bm25_only / no_queries）→ 明写
      「维持 `search.rrf.k: 60` 不变」+ 根因数据，**绝不**把 tie-break 的
      best_params 包装成推荐值
    - `vector_available=False` → 顶部先打降级警示，声明结论不可当正常结论使用
    """
    lines: list[str] = []
    lines.append("## 🔎 RRF 检索调参")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|----|----|")
    lines.append(f"| GT 派生模式 | `{rrf.gt_mode}` |")
    lines.append(f"| 查询数 | {rrf.query_count} |")
    lines.append(f"| 语料规模（记忆条数） | {rrf.corpus_size} |")
    lines.append(
        f"| 向量通路 | {'✅ 可用（双路 RRF）' if rrf.vector_available else '❌ 不可用（退化单路 BM25）'} |"
    )
    lines.append(
        f"| 向量覆盖 | {rrf.vector_count}/{rrf.corpus_size}"
        f"（{rrf.vector_coverage:.2%}）★ 仅 100% 才算可用 |"
    )
    if rrf.banner_reason:
        lines.append(f"| 不可用原因 `banner_reason` | `{rrf.banner_reason}` |")
    lines.append(f"| 参数组合数 | {len(rrf.all_results)} |")
    lines.append("")

    # 降级警示：单路时 rrf_k 天然无影响，结论不可当正常结论
    if not rrf.vector_available:
        lines.append("> ⚠️ **向量通路不可用警示**")
        lines.append(">")
        lines.append(f"> 原因（`banner_reason`）：`{rrf.banner_reason or 'unknown'}`。")
        lines.append(_banner_explain(rrf))
        lines.append("> RRF 退化为**单路**（仅 BM25）。单路下 `rrf_k` 只是给同一条排序")
        lines.append("> 乘一个单调正系数，**对排序与 NDCG 数学上必然无影响**。")
        lines.append("> 因此下方「无区分度」结论 **不能** 被解读为「rrf_k 参数无用」，")
        lines.append("> 它只说明本次运行没有产生有效的双路融合场景。")
        lines.append("")

    # 各 k 的 NDCG 表
    if rrf.all_results:
        param_names = [n for n in rrf.all_results[0] if n not in
                       ("ndcg10", "ndcg5", "ndcg_k", "query_count")]
        header = "| " + " | ".join(param_names) + \
                 f" | NDCG@{rrf.ndcg_k} | NDCG@5 | |"
        sep = "|" + "|".join(["------"] * (len(param_names) + 3)) + "|"
        lines.append(header)
        lines.append(sep)
        best_key = tuple(rrf.best_params.get(n) for n in param_names)
        for row in rrf.all_results:
            cells = [str(row.get(n)) for n in param_names]
            is_best = tuple(row.get(n) for n in param_names) == best_key
            mark = "⬅️ tie-break 首位" if is_best else ""
            lines.append(
                "| " + " | ".join(cells) +
                f" | {row.get('ndcg10', 0.0):.4f} | {row.get('ndcg5', 0.0):.4f} | {mark} |"
            )
        lines.append("")

    # T-129：召回率 @k 块（阶段二 A/B 护栏核心指标）
    if rrf.recall_at_k is not None:
        rk = rrf.recall_at_k
        lines.append("### 召回率 @k（T-129 A/B 护栏）")
        lines.append("")
        lines.append("| 截断位 k | recall@k |")
        lines.append("|----------|----------|")
        lines.append(f"| @1 | {rk.recall_at_1:.4f} |")
        lines.append(f"| @3 | {rk.recall_at_3:.4f} |")
        lines.append(f"| @5 | {rk.recall_at_5:.4f} |")
        lines.append(f"| @10 | {rk.recall_at_10:.4f} |")
        lines.append(f"| 有效查询数 | {rk.query_count} |")
        lines.append("")
        lines.append("> recall@k = 前 k 条结果命中「相关集」的查询占比（逐 query 平均）。")
        lines.append("> multi-hop 类 query 的 @5/@10 增益是图召回（T-134）的主要判据；")
        lines.append("> 相关集非空是该指标有意义的前提。")
        lines.append("")

    # 诊断块
    lines.append("### 区分度诊断")
    lines.append("")
    lines.append("| 诊断项 | 值 |")
    lines.append("|--------|----|")
    lines.append(f"| NDCG 极差 `ndcg_spread` | {rrf.ndcg_spread:.6f} |")
    lines.append("| 区分度阈值 `EPS`（旧布尔口径） | 0.000001 |")
    lines.append("| conclusion 判定阈值 `NDCG_SIG` | 0.01 |")
    lines.append(f"| 是否有区分度（旧布尔口径） | {'✅ 是' if rrf.discriminative else '❌ 否'} |")
    lines.append(f"| 排序敏感查询占比 `rank_sensitive_ratio` | {rrf.rank_sensitive_ratio:.4f} |")
    lines.append(
        f"| 两路 Jaccard `route_overlap_jaccard` | {rrf.route_overlap_jaccard:.4f} "
        f"（同源基线 0.694 / 解耦基线 0.121） |"
    )
    lines.append(f"| 结论 `conclusion` | `{rrf.conclusion}` |")
    lines.append(
        f"| 推荐 k `recommended_k` | {rrf.recommended_k if rrf.recommended_k is not None else '—（无）'} |"
    )
    lines.append("")

    if rrf.discriminative and rrf.recommended_k is not None:
        lines.append(f"> ✅ **有区分度（conclusive）**：`rrf_k={rrf.recommended_k}` 在本评测集上")
        lines.append(f"> 取得最高 NDCG@{rrf.ndcg_k} = {rrf.best_ndcg10:.4f}（极差 {rrf.ndcg_spread:.6f}）。")
        lines.append("")
        lines.append("可直接写入 `config/sgme.yaml`：")
        lines.append("")
        lines.append("```yaml")
        lines.append("search:")
        lines.append("  rrf:")
        lines.append(f"    k: {rrf.recommended_k}")
        lines.append("```")
        lines.append("")
        lines.append(f"（等价单行写法：`search.rrf.k: {rrf.recommended_k}`）")
        lines.append("")
    elif rrf.conclusion == "conclusive":
        # 兜底：conclusive 但 recommended_k 缺失（极端空参数空间）——不渲染推荐配置块
        lines.append(f"> ✅ **conclusive**：存在真实最优 k，NDCG@{rrf.ndcg_k} = "
                     f"{rrf.best_ndcg10:.4f}（极差 {rrf.ndcg_spread:.6f} ≥ NDCG_SIG=0.01）。")
        lines.append("> **结论：可按 `best_params` 写入 `search.rrf.k`。**")
        lines.append("")
    elif rrf.conclusion == "inconclusive_no_effect":
        lines.append("> 🔴 **inconclusive_no_effect：低重叠（BM25 退化）致 k 无作用点，确定结论。**")
        lines.append(">")
        lines.append(f"> 全部 {len(rrf.all_results)} 个参数组合的 NDCG 极差为 "
                     f"`{rrf.ndcg_spread:.6f}` < `NDCG_TIE=1e-9`，即**完全无作用点**。")
        lines.append("> 上表 tie-break 首位仅是「NDCG 相同则取更小 k」的确定性形式产物，")
        lines.append("> **不是**推荐值，不得据此调参。")
        lines.append(">")
        lines.extend(
            _jaccard_verdict(
                rrf.route_overlap_jaccard,
                int((rrf.recall_diagnostics or {}).get("dual_route_queries", 0)),
                rrf.recall_diagnostics or {},
            )
        )
        lines.append(">")
        lines.append("> **结论：维持 `search.rrf.k: 60` 不变。**")
        lines.append("")
    elif rrf.conclusion == "inconclusive_below_noise":
        lines.append("> 🟡 **inconclusive_below_noise：微弱灵敏度落噪声内，无法确认。**")
        lines.append(">")
        lines.append(f"> NDCG 极差 `{rrf.ndcg_spread:.6f}` ∈ (0, NDCG_SIG=0.01)，"
                     "存在极弱排序变化但不足以确认真实最优 k。")
        lines.append("> 上表 tie-break 首位仅是形式产物，**不是**推荐值。")
        lines.append(">")
        lines.append("> **结论：维持 `search.rrf.k: 60` 不变。**")
        lines.append("")
    elif rrf.conclusion == "inconclusive_bm25_only":
        lines.append("> ⚠️ **inconclusive_bm25_only：向量通路未起，融合未跑，无数据。**")
        lines.append(">")
        lines.append("> 与 `no_queries`（无查询）不同：本状态有查询，但 `vector_available=false`，")
        lines.append("> RRF 退化为单路 BM25，`rrf_k` 数学上必然无影响，**无法**给出推荐值。")
        lines.append(">")
        lines.append("> **结论：维持 `search.rrf.k: 60` 不变。**")
        lines.append("")
    elif rrf.conclusion == "no_queries":
        lines.append("> ⚠️ **no_queries**：未派生出任何检索查询，网格搜索未实际执行。")
        lines.append(">")
        lines.append("> **结论：维持 `search.rrf.k: 60` 不变。**")
        lines.append("")
    elif rrf.conclusion == "error":
        lines.append("> 🔴 **error**：RRF 阶段执行异常，指标不可用。")
        lines.append(">")
        lines.append("> **结论：维持 `search.rrf.k: 60` 不变。**")
        lines.append("")

    # 根因诊断数据
    diag = rrf.recall_diagnostics or {}
    if diag:
        lines.append("### 根因诊断数据（为什么无区分度）")
        lines.append("")
        lines.append("| 指标 | 值 | 说明 |")
        lines.append("|------|----|------|")
        lines.append(f"| 缓存查询数 | {diag.get('cached_queries', 0)} | 实际执行的双路召回次数 |")
        lines.append(f"| BM25 平均召回 | {diag.get('bm25_avg_recall', 0.0)} | 每 query 命中的记忆条数均值 |")
        lines.append(f"| BM25 召回中位数 | {diag.get('bm25_median_recall', 0.0)} | 中位仅 1 条 ⇒ 排序几乎无可重排空间 |")
        lines.append(f"| BM25 最大召回 | {diag.get('bm25_max_recall', 0)} | — |")
        lines.append(f"| 向量平均召回 | {diag.get('vector_avg_recall', 0.0)} | 0 表示向量通路未产出 |")
        lines.append(f"| 两路交集均值（条数） | {diag.get('route_overlap_avg', 0.0)} | RRF 分数累加只发生在交集上 |")
        lines.append(
            f"| 两路 Jaccard（top-{diag.get('route_overlap_top_n', 20)}） "
            f"| {diag.get('route_overlap_jaccard', 0.0)} | 主口径：`|∩|/|∪|` 对全部 query 取均值 |"
        )
        lines.append(
            f"| 两路 Jaccard（top-10） | {diag.get('route_overlap_jaccard_top10', 0.0)} "
            "| 与 NDCG@10 同截断位的对照口径 |"
        )
        lines.append(
            f"| 两路 Jaccard（仅双路非空） | {diag.get('route_overlap_jaccard_dual', 0.0)} "
            f"| 样本 {diag.get('jaccard_dual_query_count', 0)} 条；可与 PM 基线 0.694/0.121 直比 |"
        )
        lines.append(f"| 双路均非空的查询数 | {diag.get('dual_route_queries', 0)} | 只有这些查询的 `rrf_k` 才可能生效 |")
        lines.append(f"| BM25 空召回查询数 | {diag.get('queries_with_empty_bm25', 0)} | 该 query 只剩单路 ⇒ `rrf_k` 对其排序数学上无影响 |")
        if "vector_failed_at" in diag:
            lines.append(
                f"| 向量熔断位置 | 第 {int(diag['vector_failed_at']) + 1} 条 "
                "| 任意一条嵌入失败即熔断，避免产出部分覆盖语料 |"
            )
        if "error" in diag:
            lines.append(f"| 异常 | `{diag['error']}` | — |")
        lines.append("")
        lines.append("> 根因：FTS5 `unicode61` tokenizer 对中文按标点整段切 token，")
        lines.append("> 召回极窄（BM25 中位仅 1 条）。候选集只有 1 条时任何融合参数都改不了排序，")
        lines.append("> 这是 **BM25 退化**导致的「伪解耦」——两路并非各说各话，")
        lines.append("> 而是低重叠系列表长度悬殊伪影，不是「参数无用」的证据。")
        lines.append("")

    # embedding 缓存（可复现性基础设施）
    cache = rrf.embed_cache or {}
    if cache:
        lines.append("### embedding 缓存")
        lines.append("")
        lines.append("| 项 | 值 |")
        lines.append("|----|----|")
        lines.append(f"| 命中 | {cache.get('hits', 0)} |")
        lines.append(f"| 未命中 | {cache.get('misses', 0)} |")
        lines.append(f"| 新写入 | {cache.get('writes', 0)} |")
        lines.append(f"| dims 不匹配（判为未命中） | {cache.get('dims_mismatch', 0)} |")
        lines.append(f"| 库内条目 | {cache.get('rows', 0)} |")
        lines.append("")
        lines.append("> 缓存 key = `sha256(content) + model`，命中即零网络。")
        lines.append("> `未命中 = 0` 时本次 run 完全离线可复现；`新写入 > 0` 说明")
        lines.append("> 语料或查询有变动，需重新归档 `eval/fixtures/embed_cache_v001.sqlite`。")
        lines.append("")

    return lines


def _status_emoji(value: float, target: float) -> str:
    """根据值 vs 目标返回状态 emoji。"""
    if value >= target:
        return "🟢 达标"
    elif value >= target * 0.75:
        return "🟡 接近"
    else:
        return "🔴 不达标"
