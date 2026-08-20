"""eval/metrics.py：度量计算器。

- compute_l1_f1: 内容相似度贪心匹配 + 微平均 F1（主指标）
- compute_l1_strict_match: 严格完全匹配率
- compute_subsidiary_acc: memory_type / time_velocity / priority_mae
- compute_l2_section_hitrate: 模板查询 Section 命中率
- compute_profile_quality: F1 × hitrate

L1 F1 计算链路详见 docs/design/SGME-评测框架设计-v0.1.md §1.3。
"""

from __future__ import annotations

import difflib
import logging
import math
from collections import defaultdict

from eval.models import (
    CaseResult,
    DimensionF1,
    GtMemory,
    InjectGroundTruth,
    InjectMetrics,
    L1GroundTruth,
    L1Metrics,
    L2GroundTruth,
    L2Metrics,
)

logger = logging.getLogger("eval.metrics")

# 内容相似度匹配阈值（difflib.SequenceMatcher.ratio）
MATCH_THRESHOLD = 0.5


# ── 内容相似度 ──

def _content_similarity(a: str, b: str) -> float:
    """计算两段文本的内容相似度（difflib.SequenceMatcher）。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()


# ── 记忆匹配 ──

def _match_memories(
    pred_memories: list[dict],
    gt_memories: list[GtMemory],
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """贪心最大匹配：预测记忆 ↔ ground truth 记忆。

    使用内容相似度构建二分图，贪心选取最高相似度配对。
    每个预测最多匹配一个 GT，每个 GT 最多匹配一个预测。
    相似度 < MATCH_THRESHOLD 不匹配。

    返回：
    - matched_pairs: [(pred_index, gt_index), ...]
    - unmatched_preds: [pred_index, ...]   → 视为 FP 记忆
    - unmatched_gts: [gt_index, ...]       → 视为 FN 记忆
    """
    n_pred = len(pred_memories)
    n_gt = len(gt_memories)

    if n_pred == 0 and n_gt == 0:
        return [], [], []
    if n_pred == 0:
        return [], [], list(range(n_gt))
    if n_gt == 0:
        return [], list(range(n_pred)), []

    # 计算所有配对相似度，过滤低于阈值的
    pairs: list[tuple[float, int, int]] = []  # (similarity, pred_i, gt_j)
    for i, pred in enumerate(pred_memories):
        pred_content = pred.get("content", "")
        for j, gt in enumerate(gt_memories):
            sim = _content_similarity(pred_content, gt.content)
            if sim >= MATCH_THRESHOLD:
                pairs.append((sim, i, j))

    # 按相似度降序排序
    pairs.sort(key=lambda x: x[0], reverse=True)

    matched_preds: set[int] = set()
    matched_gts: set[int] = set()
    matched_pairs: list[tuple[int, int]] = []

    for _, pred_i, gt_j in pairs:
        if pred_i not in matched_preds and gt_j not in matched_gts:
            matched_preds.add(pred_i)
            matched_gts.add(gt_j)
            matched_pairs.append((pred_i, gt_j))

    unmatched_preds = [i for i in range(n_pred) if i not in matched_preds]
    unmatched_gts = [j for j in range(n_gt) if j not in matched_gts]

    return matched_pairs, unmatched_preds, unmatched_gts


# ── 维度级 TP/FP/FN ──

def _compute_dimension_tp_fp_fn(
    matched_pairs: list[tuple[int, int]],
    pred_memories: list[dict],
    gt_memories: list[GtMemory],
    unmatched_preds: list[int],
    unmatched_gts: list[int],
) -> tuple[int, int, int, dict[str, dict[str, int]]]:
    """计算维度级 TP/FP/FN。

    对匹配对：TP += |pred_dims ∩ gt_dims|, FP += |pred_dims - gt_dims|, FN += |gt_dims - pred_dims|
    对 unmatched_preds：FP += Σ|pred.dimension_ids|
    对 unmatched_gts：FN += Σ|gt.dimensions|

    返回 (total_tp, total_fp, total_fn, per_dim_stats)
    其中 per_dim_stats = {dim_id: {"tp": n, "fp": n, "fn": n}}
    """
    total_tp = 0
    total_fp = 0
    total_fn = 0
    per_dim: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0}
    )

    for pred_i, gt_j in matched_pairs:
        pred_dims = set(pred_memories[pred_i].get("dimension_ids",
                          pred_memories[pred_i].get("dimensions", [])))
        gt_dims = set(gt_memories[gt_j].dimensions)

        tp_dims = pred_dims & gt_dims
        fp_dims = pred_dims - gt_dims
        fn_dims = gt_dims - pred_dims

        total_tp += len(tp_dims)
        total_fp += len(fp_dims)
        total_fn += len(fn_dims)

        for d in tp_dims:
            per_dim[d]["tp"] += 1
        for d in fp_dims:
            per_dim[d]["fp"] += 1
        for d in fn_dims:
            per_dim[d]["fn"] += 1

    for pred_i in unmatched_preds:
        pred_dims = pred_memories[pred_i].get("dimension_ids",
                      pred_memories[pred_i].get("dimensions", []))
        total_fp += len(pred_dims)
        for d in pred_dims:
            per_dim[d]["fp"] += 1

    for gt_j in unmatched_gts:
        gt_dims = gt_memories[gt_j].dimensions
        total_fn += len(gt_dims)
        for d in gt_dims:
            per_dim[d]["fn"] += 1

    return total_tp, total_fp, total_fn, dict(per_dim)


# ── 逐维度 F1 ──

def _compute_per_dimension_f1(per_dim_stats: dict[str, dict[str, int]]) -> dict[str, DimensionF1]:
    """根据逐维度 TP/FP/FN 计算逐维度 F1。"""
    result: dict[str, DimensionF1] = {}
    for dim_id, stats in per_dim_stats.items():
        tp = stats["tp"]
        fp = stats["fp"]
        fn = stats["fn"]
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        result[dim_id] = DimensionF1(
            dimension_id=dim_id,
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
            tp=tp, fp=fp, fn=fn,
        )
    return result


# ── L1 主指标 ──

def compute_l1_f1(
    predictions: list[dict],
    ground_truth: L1GroundTruth,
) -> L1Metrics:
    """计算 L1 维度标注完整指标。

    predictions: L1 引擎输出（已归一的维度 id 列表），每条含:
      - content, dimension_ids (或 dimensions), memory_type, priority, time_velocity
    ground_truth: expected_l1（评测用例的标注答案）

    返回 L1Metrics（含微平均 F1、逐维度 F1、Strict Match、辅助指标）。
    """
    gt_memories = ground_truth.memories
    n_cases = 1  # 单条用例

    # ① 记忆匹配
    matched_pairs, unmatched_preds, unmatched_gts = _match_memories(
        predictions, gt_memories,
    )

    # ② 维度级 TP/FP/FN
    total_tp, total_fp, total_fn, per_dim_stats = _compute_dimension_tp_fp_fn(
        matched_pairs, predictions, gt_memories,
        unmatched_preds, unmatched_gts,
    )

    # ③ 微平均 P/R/F1
    micro_precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    micro_recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if (micro_precision + micro_recall) > 0 else 0.0
    )

    # ④ 逐维度 F1
    per_dim_f1 = _compute_per_dimension_f1(per_dim_stats)

    # ⑤ Strict Match
    strict_match = compute_l1_strict_match(predictions, ground_truth)

    # ⑥ 辅助指标（仅对匹配对计算）
    mt_acc, tv_acc, pri_mae = compute_subsidiary_acc(
        predictions, ground_truth, matched_pairs,
    )

    return L1Metrics(
        dimension_micro_f1=round(micro_f1, 4),
        dimension_micro_precision=round(micro_precision, 4),
        dimension_micro_recall=round(micro_recall, 4),
        per_dimension_f1=per_dim_f1,
        strict_match_rate=1.0 if strict_match else 0.0,
        memory_type_accuracy=round(mt_acc, 4),
        time_velocity_accuracy=round(tv_acc, 4),
        priority_mae=round(pri_mae, 4),
        total_tp=total_tp,
        total_fp=total_fp,
        total_fn=total_fn,
    )


def compute_l1_strict_match(
    predictions: list[dict],
    ground_truth: L1GroundTruth,
) -> bool:
    """判定单条用例是否为严格完全匹配。

    条件：
    - 预测记忆数 == GT 记忆数
    - 无 unmatched_preds 和 unmatched_gts
    - 每对匹配记忆的维度集合完全一致
    """
    gt_memories = ground_truth.memories

    if len(predictions) != len(gt_memories):
        return False

    matched_pairs, unmatched_preds, unmatched_gts = _match_memories(
        predictions, gt_memories,
    )

    if unmatched_preds or unmatched_gts:
        return False

    if len(matched_pairs) != len(gt_memories):
        return False

    for pred_i, gt_j in matched_pairs:
        pred_dims = set(predictions[pred_i].get("dimension_ids",
                         predictions[pred_i].get("dimensions", [])))
        gt_dims = set(gt_memories[gt_j].dimensions)
        if pred_dims != gt_dims:
            return False

    return True


def compute_subsidiary_acc(
    predictions: list[dict],
    ground_truth: L1GroundTruth,
    matched_pairs: list[tuple[int, int]],
) -> tuple[float, float, float]:
    """计算辅助指标：memory_type Accuracy, time_velocity Accuracy, priority MAE。

    仅对已匹配的记忆对计算。返回 (mt_acc, tv_acc, pri_mae)。
    """
    if not matched_pairs:
        return 0.0, 0.0, 0.0

    gt_memories = ground_truth.memories
    mt_correct = 0
    tv_correct = 0
    pri_sum = 0.0

    for pred_i, gt_j in matched_pairs:
        pred = predictions[pred_i]
        gt = gt_memories[gt_j]

        if pred.get("memory_type") == gt.memory_type:
            mt_correct += 1
        if pred.get("time_velocity") == gt.time_velocity:
            tv_correct += 1
        pri_sum += abs(int(pred.get("priority", 50)) - gt.priority)

    n = len(matched_pairs)
    return mt_correct / n, tv_correct / n, pri_sum / n


# ── L2 指标 ──

def compute_l2_section_hitrate(
    template_results: dict[str, str],
    ground_truth: L2GroundTruth | None,
    mode: str,
    l1_f1: float = 0.0,
) -> L2Metrics:
    """计算 L2 模板查询 Section 命中率。

    template_results: {memory_index: actual_section_title}
    ground_truth.expected_l2.template_section[mode]: {memory_index: expected_section_title}

    返回 L2Metrics（section_hit_rate / misentry_rate / miss_rate / profile_quality）。
    """
    if ground_truth is None or not ground_truth.template_section:
        return L2Metrics(total_evaluated=0)

    mode_gt = ground_truth.template_section.get(mode, {})
    if not mode_gt:
        return L2Metrics(total_evaluated=0)

    total_expected = len(mode_gt)
    hit = 0
    miss = 0

    for mem_idx, expected_section in mode_gt.items():
        actual = template_results.get(mem_idx)
        if actual is None:
            miss += 1
        elif actual == expected_section:
            hit += 1
        # else: wrong section (misentry counted below)

    total_returned = len(template_results)
    # 误入 = 返回了但不在 GT 中的，或返回了但 section 不对的
    misentry = 0
    for mem_idx, actual in template_results.items():
        if mem_idx not in mode_gt:
            misentry += 1
        elif actual != mode_gt[mem_idx]:
            misentry += 1

    hit_rate = hit / total_expected if total_expected > 0 else 0.0
    misentry_rate = misentry / total_returned if total_returned > 0 else 0.0
    miss_rate = miss / total_expected if total_expected > 0 else 0.0
    profile_quality = compute_profile_quality(l1_f1, hit_rate)

    return L2Metrics(
        section_hit_rate=round(hit_rate, 4),
        section_misentry_rate=round(misentry_rate, 4),
        section_miss_rate=round(miss_rate, 4),
        profile_quality=round(profile_quality, 4),
        total_evaluated=total_expected,
    )


def compute_profile_quality(l1_f1: float, l2_hitrate: float) -> float:
    """画像质量 = L1 维度 F1 × L2 Section 命中率。

    依据 PRD §5.3：ProfileQuality = L1_Dimension_F1 × L2_Section_HitRate
    """
    return l1_f1 * l2_hitrate


# ── 聚合多个用例的度量 ──

def aggregate_l1_metrics(per_case_results: list[L1Metrics]) -> L1Metrics:
    """聚合多条用例的 L1 指标（微平均）。"""
    if not per_case_results:
        return L1Metrics()

    total_tp = sum(m.total_tp for m in per_case_results)
    total_fp = sum(m.total_fp for m in per_case_results)
    total_fn = sum(m.total_fn for m in per_case_results)
    n = len(per_case_results)

    micro_precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    micro_recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if (micro_precision + micro_recall) > 0 else 0.0
    )

    # 聚合逐维度统计
    all_per_dim: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0}
    )
    for m in per_case_results:
        for dim_id, df1 in m.per_dimension_f1.items():
            all_per_dim[dim_id]["tp"] += df1.tp
            all_per_dim[dim_id]["fp"] += df1.fp
            all_per_dim[dim_id]["fn"] += df1.fn

    per_dim_f1 = _compute_per_dimension_f1(dict(all_per_dim))

    strict_matches = sum(1 for m in per_case_results if m.strict_match_rate > 0.5)
    strict_rate = strict_matches / n if n > 0 else 0.0

    # 加权平均辅助指标
    mt_acc = sum(m.memory_type_accuracy for m in per_case_results) / n if n > 0 else 0.0
    tv_acc = sum(m.time_velocity_accuracy for m in per_case_results) / n if n > 0 else 0.0
    pri_mae = sum(m.priority_mae for m in per_case_results) / n if n > 0 else 0.0

    return L1Metrics(
        dimension_micro_f1=round(micro_f1, 4),
        dimension_micro_precision=round(micro_precision, 4),
        dimension_micro_recall=round(micro_recall, 4),
        per_dimension_f1=per_dim_f1,
        strict_match_rate=round(strict_rate, 4),
        memory_type_accuracy=round(mt_acc, 4),
        time_velocity_accuracy=round(tv_acc, 4),
        priority_mae=round(pri_mae, 4),
        total_tp=total_tp,
        total_fp=total_fp,
        total_fn=total_fn,
    )


def aggregate_l2_metrics(per_case_l2: list[L2Metrics], l1_f1: float) -> L2Metrics:
    """聚合多条用例的 L2 指标。"""
    if not per_case_l2:
        return L2Metrics()

    total_evaluated = sum(m.total_evaluated for m in per_case_l2)
    total_hit = sum(
        int(m.section_hit_rate * m.total_evaluated) for m in per_case_l2
    )
    total_returned = sum(
        int(m.section_misentry_rate * m.total_evaluated + m.total_evaluated)
        if m.section_misentry_rate > 0 and m.total_evaluated > 0
        else m.total_evaluated
        for m in per_case_l2
    )

    hit_rate = total_hit / total_evaluated if total_evaluated > 0 else 0.0
    # 简化：用各 case 平均作为聚合
    hit_rate = (
        sum(m.section_hit_rate * m.total_evaluated for m in per_case_l2) / total_evaluated
        if total_evaluated > 0 else 0.0
    )
    misentry_rate = (
        sum(m.section_misentry_rate for m in per_case_l2) / len(per_case_l2)
        if per_case_l2 else 0.0
    )
    miss_rate = (
        sum(m.section_miss_rate for m in per_case_l2) / len(per_case_l2)
        if per_case_l2 else 0.0
    )

    return L2Metrics(
        section_hit_rate=round(hit_rate, 4),
        section_misentry_rate=round(misentry_rate, 4),
        section_miss_rate=round(miss_rate, 4),
        profile_quality=round(compute_profile_quality(l1_f1, hit_rate), 4),
        total_evaluated=total_evaluated,
    )


# ── 注入效果指标（T-20，PRD §5.4） ──

def _memory_index_from_id(memory_id: str, case_id: str) -> int | None:
    """从确定性 memory_id（格式 {case_id}#{idx}）解析 GT 记忆索引。

    与 retrieval_gt.memory_id_for 同规则：f"{case_id}#{idx}"。
    解析失败返回 None（不匹配即忽略，不误判）。
    """
    if not memory_id:
        return None
    prefix = f"{case_id}#"
    if not memory_id.startswith(prefix):
        return None
    idx_str = memory_id[len(prefix):]
    try:
        return int(idx_str)
    except ValueError:
        return None


def compute_inject_metrics(
    blocks: list[dict],
    ground_truth: InjectGroundTruth,
    case_id: str = "",
) -> InjectMetrics:
    """计算注入效果指标（PRD §5.4）。

    blocks: 注入响应（build_inject_blocks 输出），每个 block:
      - title / items[]（含 content、memory_id）/ present
    ground_truth: expected_inject（mode / subsequent_conversation / referenced_memory_indices）

    判定（零 LLM，ground truth 驱动）：
    - 相关块 = present=true 且块内含 ≥1 条 memory_id 索引 ∈ referenced_memory_indices 的记忆
    - 注入命中率 = 相关块数 / present 块总数
    - 引用覆盖率 = 命中且被引用的记忆数 / 被引用记忆总数
    """
    referenced = set(ground_truth.referenced_memory_indices)
    total_referenced = len(referenced)
    if total_referenced == 0:
        return InjectMetrics()  # 无引用标注 → 全部 0（不除零）

    present_blocks = [b for b in blocks if b.get("present")]
    total_blocks = len(present_blocks)

    relevant_blocks = 0
    hit_and_referenced = 0
    referenced_hit_seen: set[int] = set()

    for block in present_blocks:
        block_relevant = False
        for item in block.get("items", []):
            idx = _memory_index_from_id(item.get("memory_id", ""), case_id)
            if idx is None or idx not in referenced:
                continue
            # 被引用且注入命中
            if idx not in referenced_hit_seen:
                referenced_hit_seen.add(idx)
                hit_and_referenced += 1
            block_relevant = True
        if block_relevant:
            relevant_blocks += 1

    inject_hit_rate = relevant_blocks / total_blocks if total_blocks > 0 else 0.0
    reference_coverage = hit_and_referenced / total_referenced

    return InjectMetrics(
        inject_hit_rate=round(inject_hit_rate, 4),
        reference_coverage=round(reference_coverage, 4),
        total_blocks=total_blocks,
        relevant_blocks=relevant_blocks,
        total_referenced=total_referenced,
        hit_and_referenced=hit_and_referenced,
    )


def aggregate_inject_metrics(per_case_metrics: list[InjectMetrics]) -> InjectMetrics:
    """聚合多条用例的注入效果指标（分子/分母分别求和）。

    与 aggregate_l1_metrics 同哲学：不平均各 case 的比率，
    而是先汇总分子分母再算整体比率（避免小 case 被大 case 稀释）。
    """
    if not per_case_metrics:
        return InjectMetrics()

    total_blocks = sum(m.total_blocks for m in per_case_metrics)
    relevant_blocks = sum(m.relevant_blocks for m in per_case_metrics)
    total_referenced = sum(m.total_referenced for m in per_case_metrics)
    hit_and_referenced = sum(m.hit_and_referenced for m in per_case_metrics)

    inject_hit_rate = relevant_blocks / total_blocks if total_blocks > 0 else 0.0
    reference_coverage = hit_and_referenced / total_referenced if total_referenced > 0 else 0.0

    return InjectMetrics(
        inject_hit_rate=round(inject_hit_rate, 4),
        reference_coverage=round(reference_coverage, 4),
        total_blocks=total_blocks,
        relevant_blocks=relevant_blocks,
        total_referenced=total_referenced,
        hit_and_referenced=hit_and_referenced,
    )
