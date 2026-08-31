"""eval/models.py：评测数据结构定义。

字段英文；注释中文。
对应设计文档 eval-class-diagram.mermaid 的数据类定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── 评测用例数据结构 ──

@dataclass
class GtMemory:
    """ground truth 单条记忆的预期标注。

    dimensions 必须是注册表 id（英文 snake_case），
    标注时已由人工确认对应 registry/dimensions.yaml。
    """
    content: str = ""
    dimensions: list[str] = field(default_factory=list)
    memory_type: str = "persona"       # persona / episodic / instruction
    priority: int = 50                  # 0-100
    time_velocity: str = "static"       # static / dynamic
    source_message_ids: list[str] = field(default_factory=list)


@dataclass
class L1GroundTruth:
    """L1 ground truth：应提取的记忆列表与维度标注。"""
    memories: list[GtMemory] = field(default_factory=list)


@dataclass
class GtConflictAction:
    """L1.5 ground truth：单条冲突裁决预期。"""
    new_memory_index: int = 0
    candidate_ids: list[str] = field(default_factory=list)
    action: str = "store"               # store / skip / update / merge
    merged_content: Optional[str] = None
    reason: str = ""


@dataclass
class L15GroundTruth:
    """L1.5 ground truth：预期冲突提炼结果（可选）。"""
    actions: list[GtConflictAction] = field(default_factory=list)


@dataclass
class L2GroundTruth:
    """L2 ground truth：预期场景标签与模板查询匹配（可选）。

    template_section 结构：{mode_name: {memory_index: section_title}}
    如 {"daily": {"0": "👤 基本信息"}, "coding": {"0": "📦 项目进展"}}
    """
    scene_labels: list[str] = field(default_factory=list)
    template_section: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class InjectGroundTruth:
    """模板注入效果 ground truth（T-20，可选）。

    对应 PRD §5.4：注入效果 = 注入画像块对「用户后续对话」的相关性。
    - mode: 注入模式（daily/coding/work/full，对应 templates/*.yaml）
    - subsequent_conversation: 用户注入画像之后的后续对话文本
    - referenced_memory_indices: 后续对话引用了哪些 GT 记忆（expected_l1.memories 索引）
    """
    mode: str = ""
    subsequent_conversation: str = ""
    referenced_memory_indices: list[int] = field(default_factory=list)


@dataclass
class EvalCase:
    """单条评测用例。

    conversation 模拟 L0 原始层文件内容（行内 msg_id 序号标注），
    可直接喂入 L1 提炼管线。
    """
    case_id: str = ""
    source: str = "synthetic"           # real / synthetic / edge
    difficulty: str = "medium"           # easy / medium / hard
    conversation: str = ""
    expected_l1: L1GroundTruth = field(default_factory=L1GroundTruth)
    expected_l15: Optional[L15GroundTruth] = None
    expected_l2: Optional[L2GroundTruth] = None
    expected_inject: Optional[InjectGroundTruth] = None   # T-20 注入效果评测（可选）
    notes: str = ""


# ── 评测结果数据结构 ──

@dataclass
class DimensionF1:
    """逐维度 F1 明细。"""
    dimension_id: str = ""
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    tp: int = 0
    fp: int = 0
    fn: int = 0


@dataclass
class L1Metrics:
    """L1 维度标注评测指标。

    P0：dimension_micro_f1 / strict_match_rate / per_dimension_f1
    P1：memory_type_accuracy / time_velocity_accuracy
    P2：priority_mae
    """
    dimension_micro_f1: float = 0.0
    dimension_micro_precision: float = 0.0
    dimension_micro_recall: float = 0.0
    per_dimension_f1: dict[str, DimensionF1] = field(default_factory=dict)
    strict_match_rate: float = 0.0
    memory_type_accuracy: float = 0.0
    time_velocity_accuracy: float = 0.0
    priority_mae: float = 0.0
    total_tp: int = 0
    total_fp: int = 0
    total_fn: int = 0


@dataclass
class L2Metrics:
    """L2 场景/模板评测指标。"""
    section_hit_rate: float = 0.0
    section_misentry_rate: float = 0.0
    section_miss_rate: float = 0.0
    profile_quality: float = 0.0        # L1 F1 × L2 hitrate
    total_evaluated: int = 0


@dataclass
class L15Metrics:
    """L1.5 冲突提炼评测指标。"""
    action_accuracy: float = 0.0
    per_action_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class RRFMetrics:
    """RRF 检索评测指标。

    原 4 字段（best_ndcg10/best_params/all_results/param_sensitivity）保持不变；
    新增字段全部带默认值，向后兼容旧 report.json 与旧构造调用。

    ★ 诚实诊断契约（PRD §6.4.3 五值枚举）：
    `conclusion` ∈ {"not_run", "no_queries", "conclusive",
                    "inconclusive_no_effect", "inconclusive_below_noise",
                    "inconclusive_bm25_only", "error"}。判据：
      - `conclusive`：vector_available=true 且 ndcg_spread ≥ NDCG_SIG(0.01)，
        唯一允许 `recommended_k` 非 None 的状态；
      - `inconclusive_no_effect`：vector=true 且 ndcg_spread < NDCG_TIE(1e-9)（≈0），
        k 无作用点是**确定结论**（低重叠 BM25 退化致）；
      - `inconclusive_below_noise`：vector=true 且 0 < ndcg_spread < NDCG_SIG，
        微弱灵敏度落噪声内；
      - `inconclusive_bm25_only`：vector=false 且有查询（融合未跑，无数据）；
      - `no_queries`：无查询。
    除 `conclusive` 外 `recommended_k` 恒为 None，**绝不**把 tie-break 的
    best_params 当推荐值。`discriminative` 布尔字段保留（旧口径 spread≥EPS），
    只作过程诊断，不作为结论判据。

    ★ `route_overlap_jaccard` 的作用（为什么 no_effect 结论必须带上它）：
    对每条 query 取 BM25 路 top-N 的 memory_id 集合与向量路 top-N 集合，
    算 `|交集| / |并集|`，再对全部 query 求均值。低 Jaccard 的归因必须结合
    `bm25_avg_recall` 判读（PRD §6.4.3，修正归因写反 bug）：
      - Jaccard 高（≈0.7，两路几乎召回同一批）⇒ 两路同源，融合本就无信息增益，
        「无区分度」是**语料/召回同源的假象**，换 k 再多次也没用；
      - Jaccard 低（≈0.1）且 BM25 召回健康（平均 ≥ 3）⇒ 两路确实解耦，
        融合有信息增益却仍分不出优劣 ⇒ 是**评测集真的没有分辨力**；
      - Jaccard 低但 BM25 平均召回 < 3 ⇒ 低重叠是**列表长度悬殊伪影**
        （BM25 几乎不召回、向量恒满 20 条撑爆分母），真相是 **BM25 退化**，
        两路并非各说各话而是高度一致——p0-runA 实测 Jaccard=0.0746、
        bm25_avg_recall=1.74、87.4% BM25 命中被向量集合包含即属此类「伪解耦」。
    没有这个数就只能猜，结论不可采信。
    """
    # ── 原有 4 字段 ──
    best_ndcg10: float = 0.0
    best_params: dict = field(default_factory=dict)
    all_results: list[dict] = field(default_factory=list)
    param_sensitivity: dict = field(default_factory=dict)
    # ── #32 新增 ──
    ndcg_k: int = 10                     # 主评测截断位（best_ndcg10 对应的 k）
    best_ndcg5: float = 0.0              # 最优组合的 NDCG@5（extra_ks 首项）
    gt_mode: str = "message"             # GT 派生模式 message / content
    vector_available: bool = False       # 向量通路是否可用（★ 仅 100% 覆盖为 True）
    vector_count: int = 0                # 实际嵌入成功的记忆条数
    vector_coverage: float = 0.0         # vector_count / corpus_size
    banner_reason: str = ""              # 向量不可用原因（"" = 可用），见 retrieval_gt
    query_count: int = 0                 # 参与评测的查询数
    corpus_size: int = 0                 # 落库语料规模
    ndcg_spread: float = 0.0             # 各参数组合 NDCG 极差 max-min
    discriminative: bool = False         # ndcg_spread >= EPS
    rank_sensitive_ratio: float = 0.0    # 排序随 k 变化的查询占比
    route_overlap_jaccard: float = 0.0   # ★ 两路 top-N 的 Jaccard 均值（见下）
    conclusion: str = "not_run"          # 诚实结论（见类 docstring）
    recommended_k: Optional[int] = None  # 仅 discriminative=True 时非 None
    recall_diagnostics: dict = field(default_factory=dict)  # 根因数据：两路召回量/交集
    embed_cache: dict = field(default_factory=dict)         # embedding 缓存命中统计
    # ── T-129 召回率 @k（阶段二 A/B 护栏核心指标）──
    recall_at_k: Optional["RecallAtK"] = None                # recall@1/3/5/10 聚合（真实库副本基线专用）


@dataclass
class RecallAtK:
    """检索召回率 @k（T-129 阶段二 A/B 护栏核心指标）。

    recall@k = 前 k 条结果中命中「相关集」的查询占比（逐 query 平均）。
    - 相关集非空是该指标有意义的硬前提（空相关集会污染均值，见 rrf._compute_ndcg）
    - k 取 1/3/5/10：@1 看首条命中率，@10 看前 10 覆盖（与 NDCG@10 同截断位）
    - 图召回（T-134）的增益主要靠 multi-hop 类 query 在 @5/@10 上体现
    """

    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    recall_at_5: float = 0.0
    recall_at_10: float = 0.0
    query_count: int = 0                 # 参与统计的查询数（相关集非空）

    def as_dict(self) -> dict:
        """转 JSON 友好 dict（字段名带 @ 便于报告直读）。"""
        return {
            "recall@1": self.recall_at_1,
            "recall@3": self.recall_at_3,
            "recall@5": self.recall_at_5,
            "recall@10": self.recall_at_10,
            "query_count": self.query_count,
        }


@dataclass
class InjectMetrics:
    """模板注入效果评测指标（T-20，PRD §5.4）。

    - inject_hit_rate: 注入命中率 = 相关块数 / present=true 的注入块总数
      （注入的画像里有多大比例是后续对话真正用得上的）
    - reference_coverage: 引用覆盖率 = 命中且被引用的记忆数 / 被引用记忆总数
      （该注入的记忆有没有被模板查询捞出来）
    - total_blocks / relevant_blocks: 注入块总数 / 相关块数
    - total_referenced / hit_and_referenced: 引用记忆数 / 命中且引用数
    """
    inject_hit_rate: float = 0.0
    reference_coverage: float = 0.0
    total_blocks: int = 0
    relevant_blocks: int = 0
    total_referenced: int = 0
    hit_and_referenced: int = 0


@dataclass
class CaseResult:
    """逐用例评测结果。"""
    case_id: str = ""
    difficulty: str = ""
    l1_f1: float = 0.0
    strict_match: bool = False
    matched_memories: int = 0
    unmatched_pred: int = 0
    unmatched_gt: int = 0
    dimension_details: list[dict] = field(default_factory=list)
    inject_hit_rate: float = 0.0          # T-20 注入命中率（逐用例）
    inject_reference_coverage: float = 0.0  # T-20 引用覆盖率（逐用例）
    error: Optional[str] = None


@dataclass
class EvalSummary:
    """评测总览。"""
    total_cases: int = 0
    passed_p0: bool = False
    p0_status: dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0


@dataclass
class EvalResult:
    """一次完整评测运行的结果。"""
    run_id: str = ""
    timestamp: str = ""
    prompt_versions: dict = field(default_factory=dict)
    l1: Optional[L1Metrics] = None
    l2: Optional[L2Metrics] = None
    l15: Optional[L15Metrics] = None
    rrf: Optional[RRFMetrics] = None
    inject: Optional[InjectMetrics] = None   # T-20 注入效果（聚合）
    per_case: list[CaseResult] = field(default_factory=list)
    summary: EvalSummary = field(default_factory=EvalSummary)


# ── 模板查询结果辅助 ──

@dataclass
class TemplateHitRate:
    """模板查询 Section 命中率中间结果。"""
    mode: str = ""
    hit: int = 0
    misentry: int = 0
    miss: int = 0
    total_expected: int = 0
    total_returned: int = 0
