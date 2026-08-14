"""评测框架测试：models / loader / metrics / rrf / ab / runner。

覆盖 T02/T03/T04 全部模块。≥10 例。
"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import pytest

from eval import loader, metrics
from eval.ab import compare_reports, format_diff_markdown
from eval.models import (
    CaseResult,
    EvalCase,
    EvalResult,
    EvalSummary,
    GtMemory,
    L1GroundTruth,
    L1Metrics,
    L2GroundTruth,
    L2Metrics,
    RRFMetrics,
)
from eval.rrf import RRFGridSearch, compute_ndcg


# ═══════════════════════════════════════════════════
# T01: Models 基础测试
# ═══════════════════════════════════════════════════

class TestModels:
    """数据模型实例化与默认值测试。"""

    def test_gt_memory_defaults(self):
        """GtMemory 默认字段值正确。"""
        m = GtMemory()
        assert m.content == ""
        assert m.dimensions == []
        assert m.memory_type == "persona"
        assert m.priority == 50
        assert m.time_velocity == "static"

    def test_eval_case_full_construction(self):
        """EvalCase 完整构造（含 L1/L2 ground truth）。"""
        case = EvalCase(
            case_id="eval-001",
            source="synthetic",
            difficulty="easy",
            conversation="hello",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="test", dimensions=["identity"], memory_type="persona",
                         priority=80, time_velocity="static")
            ]),
            expected_l2=L2GroundTruth(
                scene_labels=["个人信息"],
                template_section={"daily": {"0": "基本信息"}},
            ),
            notes="测试用例",
        )
        assert case.case_id == "eval-001"
        assert len(case.expected_l1.memories) == 1
        assert case.expected_l2.template_section["daily"]["0"] == "基本信息"

    def test_l1_metrics_defaults(self):
        """L1Metrics 默认值全零。"""
        m = L1Metrics()
        assert m.dimension_micro_f1 == 0.0
        assert m.total_tp == 0
        assert m.total_fp == 0
        assert m.total_fn == 0


# ═══════════════════════════════════════════════════
# T02: Loader 测试
# ═══════════════════════════════════════════════════

class TestLoader:
    """评测集加载器测试。"""

    def test_load_sample_cases(self):
        """加载 v001_sample.yaml（5 条样例），校验数量和结构。"""
        sample_path = Path(__file__).parent.parent / "eval" / "cases" / "v001_sample.yaml"
        if not sample_path.exists():
            pytest.skip("v001_sample.yaml 不存在")
        cases = loader.load_cases(sample_path)
        assert len(cases) == 5
        for case in cases:
            assert case.case_id.startswith("eval-")
            assert case.source in {"real", "synthetic", "edge"}
            assert case.difficulty in {"easy", "medium", "hard"}
            assert len(case.conversation) > 0
            assert len(case.expected_l1.memories) > 0

    def test_load_nonexistent_file(self):
        """加载不存在的文件抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            loader.load_cases("/nonexistent/eval_cases.yaml")

    def test_validate_case_empty_memories(self):
        """空 memories 校验失败。"""
        case = EvalCase(
            case_id="eval-001",
            source="synthetic",
            difficulty="easy",
            conversation="hello",
            expected_l1=L1GroundTruth(memories=[]),
        )
        errors = loader.validate_case(case)
        assert len(errors) > 0
        assert any("memories" in e for e in errors)

    def test_validate_case_invalid_source(self):
        """非法 source 校验失败。"""
        case = EvalCase(
            case_id="eval-001",
            source="invalid",
            difficulty="easy",
            conversation="hello",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="test", dimensions=["identity"]),
            ]),
        )
        errors = loader.validate_case(case)
        assert any("source" in e for e in errors)

    def test_load_labels(self):
        """load_labels 返回维度注册表映射。"""
        labels = loader.load_labels()
        assert isinstance(labels, dict)
        # 至少应有 identity/tech_stack 等基础维度
        assert "identity" in labels or len(labels) > 0

    def test_get_dimension_names(self):
        """get_dimension_names 返回维度 id 列表。"""
        names = loader.get_dimension_names()
        assert isinstance(names, list)
        assert len(names) > 0


# ═══════════════════════════════════════════════════
# T02: Metrics 测试
# ═══════════════════════════════════════════════════

class TestMetricsContentSimilarity:
    """内容相似度匹配测试。"""

    def test_exact_match(self):
        """完全相同的文本相似度为 1.0。"""
        from eval.metrics import _content_similarity
        assert _content_similarity("hello world", "hello world") == 1.0

    def test_partial_match(self):
        """部分匹配相似度在 0-1 之间。"""
        from eval.metrics import _content_similarity
        sim = _content_similarity("hello world", "hello word")
        assert 0.5 < sim < 1.0

    def test_no_match(self):
        """完全不相关的文本相似度接近 0。"""
        from eval.metrics import _content_similarity
        sim = _content_similarity("hello world", "xyz abc def ghi jkl mno")
        assert sim < 0.5

    def test_empty_strings(self):
        """空字符串处理。"""
        from eval.metrics import _content_similarity
        assert metrics._content_similarity("", "") == 1.0
        assert metrics._content_similarity("hello", "") == 0.0


class TestMemoryMatching:
    """记忆匹配算法测试。"""

    def test_perfect_match(self):
        """完美匹配：相同的记忆内容一一对应。"""
        from eval.metrics import _match_memories
        preds = [
            {"content": "张明，深圳"},
            {"content": "Python 开发"},
        ]
        gts = [
            GtMemory(content="张明，深圳", dimensions=["identity"]),
            GtMemory(content="Python 开发", dimensions=["skills"]),
        ]
        matched, unmatched_preds, unmatched_gts = _match_memories(preds, gts)
        assert len(matched) == 2
        assert unmatched_preds == []
        assert unmatched_gts == []

    def test_extra_prediction(self):
        """多预测一条记忆 → 一个 unmatched_pred。"""
        from eval.metrics import _match_memories
        preds = [
            {"content": "张明"},
            {"content": "Python"},
            {"content": "extra stuff not in gt"},
        ]
        gts = [
            GtMemory(content="张明", dimensions=["identity"]),
            GtMemory(content="Python", dimensions=["skills"]),
        ]
        matched, unmatched_preds, unmatched_gts = _match_memories(preds, gts)
        assert len(matched) == 2
        assert len(unmatched_preds) == 1
        assert unmatched_gts == []

    def test_missing_prediction(self):
        """漏预测一条记忆 → 一个 unmatched_gt。"""
        from eval.metrics import _match_memories
        preds = [
            {"content": "张明"},
        ]
        gts = [
            GtMemory(content="张明", dimensions=["identity"]),
            GtMemory(content="Python", dimensions=["skills"]),
        ]
        matched, unmatched_preds, unmatched_gts = _match_memories(preds, gts)
        assert len(matched) == 1
        assert unmatched_preds == []
        assert len(unmatched_gts) == 1

    def test_below_threshold_no_match(self):
        """相似度低于阈值不匹配。"""
        from eval.metrics import _match_memories
        preds = [
            {"content": "completely different content here"},
        ]
        gts = [
            GtMemory(content="张明在深圳做后端开发", dimensions=["identity"]),
        ]
        matched, unmatched_preds, unmatched_gts = _match_memories(preds, gts)
        # 不相似的内容不应匹配
        assert len(matched) == 0
        assert len(unmatched_preds) == 1
        assert len(unmatched_gts) == 1


class TestL1F1:
    """L1 F1 计算测试。"""

    def test_perfect_f1(self):
        """完美预测 → F1 = 1.0。"""
        preds = [
            {
                "content": "张明，深圳",
                "dimension_ids": ["identity", "skills"],
                "memory_type": "persona",
                "priority": 85,
                "time_velocity": "static",
            },
        ]
        gt = L1GroundTruth(memories=[
            GtMemory(content="张明，深圳", dimensions=["identity", "skills"],
                     memory_type="persona", priority=85, time_velocity="static"),
        ])
        result = metrics.compute_l1_f1(preds, gt)
        assert result.dimension_micro_f1 == 1.0
        assert result.strict_match_rate == 1.0
        assert result.memory_type_accuracy == 1.0

    def test_partial_dimension_match(self):
        """部分维度匹配 → F1 < 1.0。"""
        preds = [
            {
                "content": "张明，深圳",
                "dimension_ids": ["identity"],  # 漏标 skills
                "memory_type": "persona",
                "priority": 85,
                "time_velocity": "static",
            },
        ]
        gt = L1GroundTruth(memories=[
            GtMemory(content="张明，深圳", dimensions=["identity", "skills"],
                     memory_type="persona", priority=85, time_velocity="static"),
        ])
        result = metrics.compute_l1_f1(preds, gt)
        # TP=1 (identity), FP=0, FN=1 (skills)
        # P=1.0, R=0.5, F1=0.6667
        assert 0.6 < result.dimension_micro_f1 < 0.7
        assert result.total_tp == 1
        assert result.total_fn == 1
        assert result.strict_match_rate == 0.0  # 维度不完全匹配

    def test_extra_dimension_fp(self):
        """多标维度 → FP > 0。"""
        preds = [
            {
                "content": "张明",
                "dimension_ids": ["identity", "skills", "tech_stack"],
                "memory_type": "persona",
                "priority": 85,
                "time_velocity": "static",
            },
        ]
        gt = L1GroundTruth(memories=[
            GtMemory(content="张明", dimensions=["identity"],
                     memory_type="persona", priority=85, time_velocity="static"),
        ])
        result = metrics.compute_l1_f1(preds, gt)
        assert result.total_tp == 1  # identity
        assert result.total_fp == 2  # skills + tech_stack
        assert result.total_fn == 0
        # P=1/3=0.333, R=1.0, F1=0.5
        assert abs(result.dimension_micro_f1 - 0.5) < 0.01

    def test_complete_mismatch(self):
        """完全错误 → F1 = 0.0。"""
        preds = [
            {
                "content": "something completely different",
                "dimension_ids": ["status"],
                "memory_type": "episodic",
                "priority": 50,
                "time_velocity": "dynamic",
            },
        ]
        gt = L1GroundTruth(memories=[
            GtMemory(content="张明在深圳", dimensions=["identity"],
                     memory_type="persona", priority=85, time_velocity="static"),
        ])
        result = metrics.compute_l1_f1(preds, gt)
        assert result.dimension_micro_f1 == 0.0
        assert result.total_tp == 0

    def test_strict_match_perfect(self):
        """严格完全匹配：预测 == GT。"""
        preds = [
            {"content": "test", "dimension_ids": ["identity", "skills"],
             "memory_type": "persona", "priority": 80, "time_velocity": "static"},
        ]
        gt = L1GroundTruth(memories=[
            GtMemory(content="test", dimensions=["identity", "skills"],
                     memory_type="persona", priority=80, time_velocity="static"),
        ])
        assert metrics.compute_l1_strict_match(preds, gt) is True

    def test_subsidiary_acc(self):
        """辅助指标计算。"""
        preds = [
            {"content": "a", "memory_type": "persona", "priority": 80, "time_velocity": "static"},
            {"content": "b", "memory_type": "episodic", "priority": 60, "time_velocity": "dynamic"},
        ]
        gt = L1GroundTruth(memories=[
            GtMemory(content="a", dimensions=["identity"], memory_type="persona", priority=80, time_velocity="static"),
            GtMemory(content="b", dimensions=["skills"], memory_type="episodic", priority=50, time_velocity="dynamic"),
        ])
        from eval.metrics import _match_memories, compute_subsidiary_acc
        matched, _, _ = _match_memories(preds, gt.memories)
        mt_acc, tv_acc, pri_mae = compute_subsidiary_acc(preds, gt, matched)
        assert mt_acc == 1.0
        assert tv_acc == 1.0
        assert abs(pri_mae - 5.0) < 0.01  # |60-50|/2=5


class TestL2Metrics:
    """L2 Section 命中率测试。"""

    def test_perfect_hit_rate(self):
        """完美命中 → hit_rate=1.0。"""
        template_results = {"0": "基本信息", "1": "项目进展"}
        gt = L2GroundTruth(
            template_section={"daily": {"0": "基本信息", "1": "项目进展"}},
        )
        result = metrics.compute_l2_section_hitrate(template_results, gt, "daily", l1_f1=0.8)
        assert result.section_hit_rate == 1.0
        assert result.profile_quality == 0.8  # 0.8 × 1.0

    def test_partial_hit_rate(self):
        """部分命中。"""
        template_results = {"0": "基本信息", "1": "技术决策"}  # 1 错了
        gt = L2GroundTruth(
            template_section={"daily": {"0": "基本信息", "1": "项目进展"}},
        )
        result = metrics.compute_l2_section_hitrate(template_results, gt, "daily", l1_f1=0.75)
        assert result.section_hit_rate == 0.5  # 1/2
        # profile_quality = 0.75 * 0.5 = 0.375
        assert abs(result.profile_quality - 0.375) < 0.01

    def test_empty_gt(self):
        """空 ground truth → 全零。"""
        result = metrics.compute_l2_section_hitrate({}, None, "daily")
        assert result.total_evaluated == 0
        assert result.section_hit_rate == 0.0

    def test_profile_quality_formula(self):
        """画像质量 = F1 × 命中率。"""
        assert metrics.compute_profile_quality(0.8, 0.7) == pytest.approx(0.56)
        assert metrics.compute_profile_quality(0.0, 1.0) == 0.0
        assert metrics.compute_profile_quality(1.0, 1.0) == 1.0


class TestAggregation:
    """聚合度量测试。"""

    def test_aggregate_l1(self):
        """聚合多条用例的 L1 指标。"""
        m1 = L1Metrics(
            dimension_micro_f1=0.8, total_tp=8, total_fp=2, total_fn=2,
            strict_match_rate=1.0, memory_type_accuracy=1.0,
            time_velocity_accuracy=1.0, priority_mae=5.0,
        )
        m2 = L1Metrics(
            dimension_micro_f1=0.6, total_tp=6, total_fp=4, total_fn=4,
            strict_match_rate=0.0, memory_type_accuracy=0.5,
            time_velocity_accuracy=0.8, priority_mae=10.0,
        )
        agg = metrics.aggregate_l1_metrics([m1, m2])
        assert agg.total_tp == 14
        assert agg.total_fp == 6
        assert agg.total_fn == 6
        # micro P = 14/20 = 0.7, micro R = 14/20 = 0.7, F1 = 0.7
        assert abs(agg.dimension_micro_f1 - 0.7) < 0.01
        assert abs(agg.strict_match_rate - 0.5) < 0.01


# ═══════════════════════════════════════════════════
# T04: RRF 测试
# ═══════════════════════════════════════════════════

class TestNDCG:
    """NDCG@k 计算测试。"""

    def test_perfect_ndcg(self):
        """完美排序 → NDCG@10 = 1.0。"""
        predicted = ["a", "b", "c", "d", "e"]
        relevant = ["a", "b", "c", "d", "e"]
        ndcg = compute_ndcg(predicted, relevant, k=10)
        assert ndcg == 1.0

    def test_reversed_ndcg(self):
        """部分不相关时逆序 → NDCG < 1.0。"""
        # 前两个不相关，后三个相关 → 排序不好
        predicted = ["x", "y", "a", "b", "c"]
        relevant = ["a", "b", "c"]
        ndcg = compute_ndcg(predicted, relevant, k=10)
        assert 0.3 < ndcg < 0.8  # 前两个位置浪费了

    def test_no_relevant(self):
        """无相关文档 → NDCG = 1.0（无相关则任何排序都是最优的）。"""
        predicted = ["a", "b", "c"]
        relevant: list[str] = []
        ndcg = compute_ndcg(predicted, relevant, k=10)
        assert ndcg == 1.0

    def test_empty_predicted(self):
        """空预测列表 → DCG = 0, NDCG = 0。"""
        predicted: list[str] = []
        relevant = ["a", "b", "c"]
        ndcg = compute_ndcg(predicted, relevant, k=10)
        assert ndcg == 0.0

    def test_k_truncation(self):
        """k 截断效果：k=2 只看前两个。"""
        # 前两个相关(c,d)，后面两个不相关(x,y) → k=2 得满分
        predicted = ["c", "d", "x", "y"]
        relevant = ["a", "b", "c", "d"]
        ndcg_10 = compute_ndcg(predicted, relevant, k=10)
        ndcg_2 = compute_ndcg(predicted, relevant, k=2)
        # k=2 时前两个都相关 (c,d)，NDCG@2 应为 1.0
        assert ndcg_2 == 1.0
        assert ndcg_10 < 1.0  # 后面两个不相关拉低了分数

    def test_class_method_alias(self):
        """RRFGridSearch.compute_ndcg 与独立函数一致。"""
        predicted = ["a", "c", "b", "d"]
        relevant = ["a", "b", "c", "d"]
        assert compute_ndcg(predicted, relevant, k=5) == \
            RRFGridSearch.compute_ndcg(predicted, relevant, k=5)


class TestRRFGridSearch:
    """RRF 网格搜索测试（#32 已真实接入，非占位）。"""

    @staticmethod
    def _constant_query_fn():
        """确定性 query_fn：rrf_k 越大返回命中越多（制造区分度以触发 discriminative）。

        纯函数：同 query + 同 params ⇒ 同结果（可复现性验收前提）。
        """
        def query_fn(q: str, params: dict) -> list[str]:
            k = int(params.get("rrf_k", 60))
            n = (k // 30) % 4   # 10→0, 30→1, 60→2, 90→3, 120→0（确定性、可复现）
            return [f"{q}#{i}" for i in range(n)]
        return query_fn

    def test_search_returns_metrics(self):
        """search() 已真实接入，返回 RRFMetrics 且字段完整、两次运行可复现。"""
        gs = RRFGridSearch()
        query_fn = self._constant_query_fn()
        gt = {"q1": ["q1#0", "q1#1", "q1#2"], "q2": ["q2#0"]}

        metrics = gs.search(query_fn, gt, k=10)
        assert isinstance(metrics, RRFMetrics)

        # 核心字段存在且类型正确
        assert metrics.ndcg_k == 10
        assert metrics.query_count == 2
        assert isinstance(metrics.all_results, list)
        assert len(metrics.all_results) == len(gs.param_space["rrf_k"])
        assert set(metrics.best_params.keys()) == {"rrf_k"}

        # 可复现：两次运行逐字段相等（诚实诊断要求 rrf 段可复现）
        m2 = RRFGridSearch().search(query_fn, gt, k=10)
        for f in metrics.__dataclass_fields__:
            assert getattr(metrics, f) == getattr(m2, f), \
                f"字段 {f} 两次运行不一致，可复现性验收失败"

    def test_status_not_started(self):
        """初始状态为 not_started，且文案不再含 'not yet implemented'。"""
        gs = RRFGridSearch()
        status = gs.status()
        assert "not_started" in status
        assert "not yet implemented" not in status.lower()
        assert gs.best_params() is None

    def test_custom_param_space(self):
        """自定义参数空间被原样保留。"""
        custom = {"rrf_k": [30, 60], "top_k": [10, 20]}
        gs = RRFGridSearch(param_space=custom)
        assert gs.param_space == custom

    def test_default_param_space(self):
        """默认参数空间仅保留 rrf_k（#32 裁剪：移除 bm25_k1/bm25_b/bm25_weight/top_k）。"""
        gs = RRFGridSearch()
        assert set(gs.param_space.keys()) == {"rrf_k"}
        assert len(gs.param_space) == 1
        assert gs.param_space["rrf_k"] == [10, 30, 60, 90, 120]


# ═══════════════════════════════════════════════════
# T04: A/B 对比测试
# ═══════════════════════════════════════════════════

class TestABCompare:
    """A/B 差分报告测试。"""

    def test_compare_dicts(self):
        """比较两份 dict report 产生 diff。"""
        report_a = {
            "run_id": "run-a",
            "l1": {
                "dimension_micro_f1": 0.75,
                "dimension_micro_precision": 0.80,
                "dimension_micro_recall": 0.70,
                "strict_match_rate": 0.50,
                "memory_type_accuracy": 0.85,
                "time_velocity_accuracy": 0.80,
                "priority_mae": 8.0,
                "per_dimension_f1": {
                    "identity": {"f1": 0.9},
                    "skills": {"f1": 0.7},
                },
            },
            "l2": {
                "section_hit_rate": 0.70,
                "section_misentry_rate": 0.15,
                "section_miss_rate": 0.15,
                "profile_quality": 0.525,
            },
        }
        report_b = {
            "run_id": "run-b",
            "l1": {
                "dimension_micro_f1": 0.78,
                "dimension_micro_precision": 0.82,
                "dimension_micro_recall": 0.74,
                "strict_match_rate": 0.55,
                "memory_type_accuracy": 0.88,
                "time_velocity_accuracy": 0.82,
                "priority_mae": 7.0,
                "per_dimension_f1": {
                    "identity": {"f1": 0.92},
                    "skills": {"f1": 0.75},
                },
            },
            "l2": {
                "section_hit_rate": 0.72,
                "section_misentry_rate": 0.13,
                "section_miss_rate": 0.12,
                "profile_quality": 0.562,
            },
        }
        diff = compare_reports(report_a, report_b)
        assert diff["l1"]["dimension_micro_f1"]["delta"] == pytest.approx(0.03)
        assert diff["l2"]["profile_quality"]["delta"] == pytest.approx(0.037)
        # 逐维度
        assert diff["l1"]["per_dimension_f1"]["identity"]["delta"] == pytest.approx(0.02)
        assert diff["l1"]["per_dimension_f1"]["skills"]["delta"] == pytest.approx(0.05)

    def test_compare_from_files(self, tmp_path):
        """从文件路径加载 report 比较。"""
        report_a = tmp_path / "report_a.json"
        report_b = tmp_path / "report_b.json"

        data_a = {
            "run_id": "a",
            "l1": {"dimension_micro_f1": 0.5, "strict_match_rate": 0.3},
            "l2": {"section_hit_rate": 0.6, "profile_quality": 0.3},
        }
        data_b = {
            "run_id": "b",
            "l1": {"dimension_micro_f1": 0.6, "strict_match_rate": 0.4},
            "l2": {"section_hit_rate": 0.7, "profile_quality": 0.42},
        }

        report_a.write_text(json.dumps(data_a), encoding="utf-8")
        report_b.write_text(json.dumps(data_b), encoding="utf-8")

        diff = compare_reports(str(report_a), str(report_b))
        assert diff["l1"]["dimension_micro_f1"]["delta"] == 0.1

    def test_format_diff_markdown(self):
        """diff 格式化为 Markdown 报告。"""
        diff = {
            "report_a": "v001",
            "report_b": "v002",
            "l1": {
                "dimension_micro_f1": {"a": 0.75, "b": 0.78, "delta": 0.03},
                "strict_match_rate": {"a": 0.50, "b": 0.55, "delta": 0.05},
            },
            "l2": {
                "section_hit_rate": {"a": 0.70, "b": 0.72, "delta": 0.02},
                "profile_quality": {"a": 0.525, "b": 0.562, "delta": 0.037},
            },
        }
        md = format_diff_markdown(diff)
        assert "A/B" in md
        assert "v001" in md
        assert "v002" in md
        assert "+0.03" in md
        assert "不做自动裁决" in md


# ═══════════════════════════════════════════════════
# T03: Runner 测试（dry-run + mock LLM）
# ═══════════════════════════════════════════════════

class TestRunnerDryRun:
    """Runner dry-run 模式测试（mock LLM，全链路自检）。"""

    @pytest.fixture
    def sample_cases(self):
        """构造 3 条简单评测用例用于 dry-run 测试。"""
        return [
            EvalCase(
                case_id="eval-001",
                source="synthetic",
                difficulty="easy",
                conversation="[msg#1] user: 我叫张三，在北京做前端开发",
                expected_l1=L1GroundTruth(memories=[
                    GtMemory(content="张三，北京，前端开发", dimensions=["identity", "skills", "tech_stack"],
                             memory_type="persona", priority=85, time_velocity="static",
                             source_message_ids=["msg_1"]),
                ]),
            ),
            EvalCase(
                case_id="eval-002",
                source="synthetic",
                difficulty="medium",
                conversation="[msg#1] user: 我在学习 Rust 和系统编程",
                expected_l1=L1GroundTruth(memories=[
                    GtMemory(content="学习 Rust", dimensions=["skills", "tech_stack"],
                             memory_type="episodic", priority=70, time_velocity="dynamic",
                             source_message_ids=["msg_1"]),
                    GtMemory(content="系统编程学习", dimensions=["tech_stack"],
                             memory_type="episodic", priority=65, time_velocity="dynamic",
                             source_message_ids=["msg_1"]),
                ]),
            ),
            EvalCase(
                case_id="eval-003",
                source="synthetic",
                difficulty="hard",
                conversation="[msg#1] user: 妈妈生日快到了要买蛋糕，那个 Rust 项目 CI 又挂了",
                expected_l1=L1GroundTruth(memories=[
                    GtMemory(content="妈妈生日快到了要买蛋糕", dimensions=["family", "habits"],
                             memory_type="episodic", priority=80, time_velocity="dynamic",
                             source_message_ids=["msg_1"]),
                    GtMemory(content="Rust 项目 CI 挂了", dimensions=["projects", "tech_stack"],
                             memory_type="episodic", priority=75, time_velocity="dynamic",
                             source_message_ids=["msg_1"]),
                ]),
            ),
        ]

    def test_runner_dry_run_basic(self, tmp_path, sample_cases):
        """dry-run 模式：加载用例 → mock LLM → 计算指标 → 输出报告。

        验证全链路可运行，不依赖真实 LLM。
        """
        from eval.runner import EvalRunner

        eval_tmp = tmp_path / "eval_tmp"
        eval_tmp.mkdir()

        runner = EvalRunner(
            cfg={},
            prompt_version=None,
            eval_tmp_dir=eval_tmp,
        )

        result = runner.run_all(sample_cases, stages=["l1"], dry_run=True)

        assert result.run_id != ""
        assert result.summary.total_cases == 3
        assert result.l1 is not None
        assert len(result.per_case) == 3
        # dry-run 每个 case 应有 l1_f1 值（mock 产生可度量输出）
        for cr in result.per_case:
            assert cr.case_id.startswith("eval-")
            assert cr.error is None

    def test_runner_generates_report_json(self, tmp_path, sample_cases):
        """runner + reporter 产出 report.json。"""
        from eval.runner import EvalRunner
        from eval.reporter import generate_report_json

        eval_tmp = tmp_path / "eval_tmp"
        eval_tmp.mkdir()
        output_dir = tmp_path / "results"
        output_dir.mkdir()

        runner = EvalRunner(
            cfg={},
            prompt_version=None,
            eval_tmp_dir=eval_tmp,
        )

        result = runner.run_all(sample_cases, stages=["l1"], dry_run=True)

        report_path = generate_report_json(result, output_dir)
        assert report_path.exists()

        with report_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["run_id"] == result.run_id
        assert data["summary"]["total_cases"] == 3
        assert "l1" in data

    def test_runner_generates_report_md(self, tmp_path, sample_cases):
        """runner + reporter 产出 report.md。"""
        from eval.runner import EvalRunner
        from eval.reporter import generate_report_md

        eval_tmp = tmp_path / "eval_tmp"
        eval_tmp.mkdir()
        output_dir = tmp_path / "results"
        output_dir.mkdir()

        runner = EvalRunner(
            cfg={},
            prompt_version=None,
            eval_tmp_dir=eval_tmp,
        )

        result = runner.run_all(sample_cases, stages=["l1"], dry_run=True)

        md_path = generate_report_md(result, output_dir)
        assert md_path.exists()

        content = md_path.read_text(encoding="utf-8")
        assert "SGME 评测基线报告" in content
        assert "L1 维度标注指标" in content

    def test_runner_clean_db_isolation(self, tmp_path, sample_cases):
        """验证 eval DB 与项目 data/ 目录物理隔离。"""
        from eval.runner import EvalRunner

        eval_tmp = tmp_path / "eval_tmp"
        eval_tmp.mkdir()
        # 验证默认 eval_tmp_dir 在 eval/tmp/ 下
        default_runner = EvalRunner(cfg={})
        default_eval_dir = default_runner.eval_tmp_dir
        assert "eval" in str(default_eval_dir).lower() or "tmp" in str(default_eval_dir).lower()
        # 不应指向 data/ 目录
        assert "data" not in str(default_eval_dir).lower().replace("eval", "")
