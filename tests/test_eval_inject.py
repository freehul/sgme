"""tests/test_eval_inject.py：模板注入效果评测测试（T-20）。

覆盖（依据 docs/design/SGME-评测基线-PRD-v0.1.md §5.4 与
SGME-评测框架设计-v0.1.md §1.7）：
1. models：InjectGroundTruth / InjectMetrics 字段与默认值
2. loader：expected_inject 解析 + schema 校验（mode 非法 / 索引越界）
3. metrics：compute_inject_metrics——注入命中率 / 引用覆盖率 / 边界（无块、无引用）
4. metrics：aggregate_inject_metrics 聚合
5. runner：inject stage 端到端（dry_run 模式，GT 落库 → 模板注入 → 度量）
6. reporter：report.json / report.md 含注入段

fixture 范式参照 tests/test_eval.py（TestRunnerDryRun）与 tests/test_demands.py。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import loader as eval_loader
from eval import metrics as eval_metrics
from eval.models import (
    EvalCase,
    GtMemory,
    InjectGroundTruth,
    InjectMetrics,
    L1GroundTruth,
    L2GroundTruth,
)


# ═══════════════════════════════════════════════════
# 1. Models 测试
# ═══════════════════════════════════════════════════

class TestInjectModels:
    """InjectGroundTruth / InjectMetrics 实例化与默认值。"""

    def test_inject_ground_truth_defaults(self):
        """InjectGroundTruth 默认字段。"""
        gt = InjectGroundTruth()
        assert gt.mode == ""
        assert gt.subsequent_conversation == ""
        assert gt.referenced_memory_indices == []

    def test_inject_ground_truth_full(self):
        """完整构造。"""
        gt = InjectGroundTruth(
            mode="coding",
            subsequent_conversation="Rust CI 挂了怎么办",
            referenced_memory_indices=[1],
        )
        assert gt.mode == "coding"
        assert gt.referenced_memory_indices == [1]

    def test_inject_metrics_defaults(self):
        """InjectMetrics 默认值全零。"""
        m = InjectMetrics()
        assert m.inject_hit_rate == 0.0
        assert m.reference_coverage == 0.0
        assert m.total_blocks == 0
        assert m.total_referenced == 0


# ═══════════════════════════════════════════════════
# 2. Loader 测试
# ═══════════════════════════════════════════════════

class TestLoaderInject:
    """expected_inject 解析与校验。"""

    @staticmethod
    def _make_yaml(tmp_path: Path, content: str) -> Path:
        p = tmp_path / "cases.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def _base_case(self) -> str:
        return """\
meta:
  version: v_test
  total_cases: 1
cases:
  - case_id: eval-900
    source: synthetic
    difficulty: easy
    conversation: |
      [msg#1] 2026-01-01T10:00:00Z user:
        我叫张明，在深圳做后端开发，用 Python 和 Go
    expected_l1:
      memories:
        - content: "张明，深圳，后端开发"
          dimensions: [identity, skills, tech_stack]
          memory_type: persona
          priority: 85
          time_velocity: static
"""

    def test_parse_expected_inject(self, tmp_path):
        """YAML 中 expected_inject 被正确解析。"""
        yaml_text = self._base_case() + """\
    expected_inject:
      mode: coding
      subsequent_conversation: |
        [msg#2] 2026-01-02T10:00:00Z user:
          SGME 自研重构进展怎么样了？
      referenced_memory_indices: [0]
"""
        cases = eval_loader.load_cases(self._make_yaml(tmp_path, yaml_text))
        assert len(cases) == 1
        inj = cases[0].expected_inject
        assert inj is not None
        assert inj.mode == "coding"
        assert "SGME 自研重构进展" in inj.subsequent_conversation
        assert inj.referenced_memory_indices == [0]

    def test_validate_invalid_mode(self, tmp_path):
        """mode 非法 → 校验失败。"""
        yaml_text = self._base_case() + """\
    expected_inject:
      mode: bogus
      subsequent_conversation: "后续对话"
      referenced_memory_indices: [0]
"""
        with pytest.raises(ValueError, match="mode"):
            eval_loader.load_cases(self._make_yaml(tmp_path, yaml_text))

    def test_validate_index_out_of_range(self, tmp_path):
        """referenced_memory_indices 越界（GT 只有 1 条记忆）→ 校验失败。"""
        yaml_text = self._base_case() + """\
    expected_inject:
      mode: coding
      subsequent_conversation: "后续对话"
      referenced_memory_indices: [5]
"""
        with pytest.raises(ValueError, match="referenced_memory_indices"):
            eval_loader.load_cases(self._make_yaml(tmp_path, yaml_text))


# ═══════════════════════════════════════════════════
# 3. Metrics 测试
# ═══════════════════════════════════════════════════

class TestComputeInjectMetrics:
    """compute_inject_metrics：注入命中率 / 引用覆盖率。"""

    def _blocks(self) -> list[dict]:
        """模拟 build_inject_blocks 输出（case_id=eval-900）。"""
        return [
            {
                "title": "🧩 技术栈与踩坑",
                "items": [
                    {"content": "张明，深圳，后端开发", "memory_id": "eval-900#0"},
                ],
                "present": True,
            },
            {
                "title": "⚙️ 工作方式",
                "items": [],
                "present": False,
            },
        ]

    def test_perfect_hit(self):
        """引用记忆全部命中 → 命中率 1.0、覆盖率 1.0。"""
        gt = InjectGroundTruth(mode="coding", subsequent_conversation="x", referenced_memory_indices=[0])
        m = eval_metrics.compute_inject_metrics(self._blocks(), gt, case_id="eval-900")
        assert m.inject_hit_rate == 1.0
        assert m.reference_coverage == 1.0
        assert m.total_blocks == 1  # 仅 present=true
        assert m.relevant_blocks == 1
        assert m.total_referenced == 1
        assert m.hit_and_referenced == 1

    def test_missed_reference(self):
        """引用记忆未被注入 → 命中率 0、覆盖率 0。"""
        gt = InjectGroundTruth(mode="coding", subsequent_conversation="x", referenced_memory_indices=[2])
        m = eval_metrics.compute_inject_metrics(self._blocks(), gt, case_id="eval-900")
        assert m.inject_hit_rate == 0.0
        assert m.reference_coverage == 0.0
        assert m.hit_and_referenced == 0

    def test_partial_hit(self):
        """部分命中：2 个引用索引在同一块 → 块相关、覆盖率按命中数算。"""
        blocks = self._blocks()
        blocks[0]["items"].append(
            {"content": "参考了 TencentDB-Agent-Memory", "memory_id": "eval-900#1"},
        )
        gt = InjectGroundTruth(mode="coding", subsequent_conversation="x", referenced_memory_indices=[0, 1])
        m = eval_metrics.compute_inject_metrics(blocks, gt, case_id="eval-900")
        assert m.inject_hit_rate == 1.0  # 两个引用都在同一块，块相关
        assert m.reference_coverage == 1.0

    def test_reference_hit_count_with_block_overflow(self):
        """引用命中记忆分布在 2 个块：命中率 1.0、覆盖率按命中数。"""
        blocks = self._blocks()
        blocks.append(
            {
                "title": "🔧 技术决策",
                "items": [
                    {"content": "Rust 系统编程", "memory_id": "eval-900#1"},
                ],
                "present": True,
            },
        )
        gt = InjectGroundTruth(mode="coding", subsequent_conversation="x", referenced_memory_indices=[0, 1])
        m = eval_metrics.compute_inject_metrics(blocks, gt, case_id="eval-900")
        assert m.total_blocks == 2
        assert m.relevant_blocks == 2
        assert m.inject_hit_rate == 1.0
        assert m.reference_coverage == 1.0

    def test_no_references(self):
        """无引用标注 → 覆盖率 0、命中率 0（不除零）。"""
        gt = InjectGroundTruth(mode="coding", subsequent_conversation="x", referenced_memory_indices=[])
        m = eval_metrics.compute_inject_metrics(self._blocks(), gt, case_id="eval-900")
        assert m.reference_coverage == 0.0
        assert m.inject_hit_rate == 0.0

    def test_no_present_blocks(self):
        """无 present 块 → 命中率 0。"""
        gt = InjectGroundTruth(mode="coding", subsequent_conversation="x", referenced_memory_indices=[0])
        m = eval_metrics.compute_inject_metrics([], gt, case_id="eval-900")
        assert m.inject_hit_rate == 0.0
        assert m.total_blocks == 0


class TestAggregateInjectMetrics:
    """aggregate_inject_metrics 聚合。"""

    def test_aggregate(self):
        """两条用例聚合：加权命中率 / 覆盖率。"""
        m1 = InjectMetrics(
            inject_hit_rate=1.0, reference_coverage=1.0,
            total_blocks=2, relevant_blocks=2,
            total_referenced=2, hit_and_referenced=2,
        )
        m2 = InjectMetrics(
            inject_hit_rate=0.5, reference_coverage=0.5,
            total_blocks=2, relevant_blocks=1,
            total_referenced=2, hit_and_referenced=1,
        )
        agg = eval_metrics.aggregate_inject_metrics([m1, m2])
        assert agg.total_blocks == 4
        assert agg.relevant_blocks == 3
        assert agg.inject_hit_rate == pytest.approx(0.75)
        assert agg.reference_coverage == pytest.approx(0.75)


# ═══════════════════════════════════════════════════
# 5. Runner 测试（inject stage 端到端）
# ═══════════════════════════════════════════════════

class TestRunnerInject:
    """Runner inject stage：GT 落库 → 模板注入 → 度量。"""

    @pytest.fixture
    def inject_case(self):
        """带 expected_inject 的用例（记忆维度覆盖 coding 模板）。"""
        return EvalCase(
            case_id="eval-901",
            source="synthetic",
            difficulty="easy",
            conversation="[msg#1] user: 我用 Python 做后端，会 Rust",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="Python 后端开发", dimensions=["tech_stack", "skills"],
                         memory_type="persona", priority=85, time_velocity="static"),
                GtMemory(content="Rust 系统编程", dimensions=["tech_stack"],
                         memory_type="episodic", priority=75, time_velocity="static"),
                GtMemory(content="代码注释用中文", dimensions=["style", "preferences"],
                         memory_type="instruction", priority=80, time_velocity="static"),
            ]),
            expected_inject=InjectGroundTruth(
                mode="coding",
                subsequent_conversation="[msg#2] user: Rust 项目 CI 挂了怎么办",
                referenced_memory_indices=[1],
            ),
        )

    def test_runner_inject_stage(self, tmp_path, inject_case):
        """inject stage 跑通：返回 InjectMetrics 且引用记忆命中。"""
        from eval.runner import EvalRunner

        eval_tmp = tmp_path / "eval_tmp"
        eval_tmp.mkdir()
        runner = EvalRunner(cfg={}, prompt_version=None, eval_tmp_dir=eval_tmp)

        result = runner.run_all([inject_case], stages=["inject"], dry_run=True)

        # EvalResult 增加 inject 字段（聚合）
        assert result.inject is not None
        assert result.inject.total_blocks >= 1
        # 引用记忆 [1] = Rust 系统编程 → coding 模板 tech_stack section 应命中
        assert result.inject.reference_coverage > 0.0
        assert 0.0 <= result.inject.inject_hit_rate <= 1.0

    def test_runner_inject_no_expected(self, tmp_path):
        """无 expected_inject 的用例不产生注入数据（inject=None）。"""
        from eval.runner import EvalRunner

        case = EvalCase(
            case_id="eval-902",
            source="synthetic",
            difficulty="easy",
            conversation="[msg#1] user: 你好",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="你好", dimensions=["identity"],
                         memory_type="persona", priority=50, time_velocity="static"),
            ]),
        )
        eval_tmp = tmp_path / "eval_tmp"
        eval_tmp.mkdir()
        runner = EvalRunner(cfg={}, prompt_version=None, eval_tmp_dir=eval_tmp)

        result = runner.run_all([case], stages=["inject"], dry_run=True)
        assert result.inject is None or result.inject.total_blocks == 0


# ═══════════════════════════════════════════════════
# 6. Reporter 测试
# ═══════════════════════════════════════════════════

class TestReporterInject:
    """report.json / report.md 含注入段。"""

    def test_report_json_has_inject(self, tmp_path):
        """report.json 包含 inject 段。"""
        from eval.runner import EvalRunner
        from eval.reporter import generate_report_json

        case = EvalCase(
            case_id="eval-903",
            source="synthetic",
            difficulty="easy",
            conversation="[msg#1] user: 我用 Python 做后端",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="Python 后端开发", dimensions=["tech_stack", "skills"],
                         memory_type="persona", priority=85, time_velocity="static"),
            ]),
            expected_inject=InjectGroundTruth(
                mode="coding",
                subsequent_conversation="[msg#2] user: Python 后端怎么部署",
                referenced_memory_indices=[0],
            ),
        )
        eval_tmp = tmp_path / "eval_tmp"
        eval_tmp.mkdir()
        output_dir = tmp_path / "results"
        output_dir.mkdir()
        runner = EvalRunner(cfg={}, prompt_version=None, eval_tmp_dir=eval_tmp)
        result = runner.run_all([case], stages=["inject"], dry_run=True)

        report_path = generate_report_json(result, output_dir)
        with report_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        assert "inject" in data
        assert "inject_hit_rate" in data["inject"]
        assert "reference_coverage" in data["inject"]


