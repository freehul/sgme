"""eval/ab.py：A/B 差分报告。

- compare_reports: 读两份 report.json → 纯离线 diff → 输出 ΔF1/Δhitrate 表格
- 不做自动裁决（与 #33 决策一致：结论留人工）

设计依据：docs/design/SGME-评测框架设计-v0.1.md §1.6。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("eval.ab")


def compare_reports(
    report_a: str | Path | dict,
    report_b: str | Path | dict,
) -> dict:
    """比较两份 report.json，输出 A/B 差分。

    report_a / report_b: report.json 文件路径或已加载的 dict
    返回 diff dict：
      {
        "report_a": str,  # 路径/标识
        "report_b": str,
        "l1": {
          "dimension_micro_f1": {"a": float, "b": float, "delta": float},
          "dimension_micro_precision": {...},
          ...
          "per_dimension_f1": {dim_id: {"a": float, "b": float, "delta": float}, ...},
        },
        "l2": {
          "section_hit_rate": {"a": float, "b": float, "delta": float},
          ...
          "profile_quality": {...},
        },
      }
    """
    # 加载 report
    data_a = _load_report(report_a)
    data_b = _load_report(report_b)

    label_a = _label_of(report_a)
    label_b = _label_of(report_b)

    l1_a = data_a.get("l1") or {}
    l1_b = data_b.get("l1") or {}
    l2_a = data_a.get("l2") or {}
    l2_b = data_b.get("l2") or {}

    diff: dict[str, Any] = {
        "report_a": label_a,
        "report_b": label_b,
        "l1": _diff_l1(l1_a, l1_b),
        "l2": _diff_l2(l2_a, l2_b),
    }

    return diff


def _load_report(report: str | Path | dict) -> dict:
    """加载 report（支持路径或已加载 dict）。"""
    if isinstance(report, dict):
        return report
    path = Path(report)
    if not path.exists():
        raise FileNotFoundError(f"report 文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _label_of(report: str | Path | dict) -> str:
    """获取 report 的显示标签。"""
    if isinstance(report, dict):
        return report.get("run_id", "(inline dict)")
    return str(report)


def _diff_l1(l1_a: dict, l1_b: dict) -> dict:
    """计算 L1 指标差分。"""

    def _delta(a_val: float, b_val: float) -> float:
        return round(b_val - a_val, 4)

    fields = [
        "dimension_micro_f1",
        "dimension_micro_precision",
        "dimension_micro_recall",
        "strict_match_rate",
        "memory_type_accuracy",
        "time_velocity_accuracy",
        "priority_mae",
    ]

    diff: dict[str, Any] = {}
    for field in fields:
        a_val = float(l1_a.get(field, 0))
        b_val = float(l1_b.get(field, 0))
        diff[field] = {
            "a": a_val,
            "b": b_val,
            "delta": _delta(a_val, b_val),
        }

    # 逐维度 F1 diff
    per_dim_a = l1_a.get("per_dimension_f1", {})
    per_dim_b = l1_b.get("per_dimension_f1", {})
    all_dims = set(per_dim_a.keys()) | set(per_dim_b.keys())

    per_dim_diff: dict[str, dict] = {}
    for dim_id in sorted(all_dims):
        df1_a = per_dim_a.get(dim_id, {})
        df1_b = per_dim_b.get(dim_id, {})
        f1_a = float(df1_a.get("f1", 0)) if isinstance(df1_a, dict) else 0.0
        f1_b = float(df1_b.get("f1", 0)) if isinstance(df1_b, dict) else 0.0
        per_dim_diff[dim_id] = {
            "a": f1_a,
            "b": f1_b,
            "delta": _delta(f1_a, f1_b),
        }
    diff["per_dimension_f1"] = per_dim_diff

    return diff


def _diff_l2(l2_a: dict, l2_b: dict) -> dict:
    """计算 L2 指标差分。"""

    def _delta(a_val: float, b_val: float) -> float:
        return round(b_val - a_val, 4)

    fields = [
        "section_hit_rate",
        "section_misentry_rate",
        "section_miss_rate",
        "profile_quality",
    ]

    diff: dict[str, Any] = {}
    for field in fields:
        a_val = float(l2_a.get(field, 0))
        b_val = float(l2_b.get(field, 0))
        diff[field] = {
            "a": a_val,
            "b": b_val,
            "delta": _delta(a_val, b_val),
        }

    return diff


def format_diff_markdown(diff: dict) -> str:
    """将 A/B diff dict 格式化为 Markdown 报告。

    用于 --compare 模式的终端输出或保存为 .md 文件。
    """
    lines: list[str] = []
    lines.append("# SGME A/B 评测差分报告")
    lines.append("")
    lines.append(f"- **A 版本**: {diff.get('report_a', '?')}")
    lines.append(f"- **B 版本**: {diff.get('report_b', '?')}")
    lines.append("")

    # L1 指标
    lines.append("## L1 维度标注指标")
    lines.append("")
    lines.append("| 指标 | A | B | Δ |")
    lines.append("|------|---|---|---|")

    l1 = diff.get("l1", {})
    l1_labels = {
        "dimension_micro_f1": "维度微平均 F1",
        "dimension_micro_precision": "维度微平均 Precision",
        "dimension_micro_recall": "维度微平均 Recall",
        "strict_match_rate": "Strict Match Rate",
        "memory_type_accuracy": "memory_type Accuracy",
        "time_velocity_accuracy": "time_velocity Accuracy",
        "priority_mae": "priority MAE",
    }

    for field, label in l1_labels.items():
        d = l1.get(field, {})
        a_val = d.get("a", "-")
        b_val = d.get("b", "-")
        delta = d.get("delta", "-")
        sign = "+" if isinstance(delta, (int, float)) and delta > 0 else ""
        delta_str = f"{sign}{delta}" if isinstance(delta, (int, float)) else str(delta)
        lines.append(f"| {label} | {a_val} | {b_val} | {delta_str} |")

    lines.append("")

    # L2 指标
    lines.append("## L2 模板查询指标")
    lines.append("")
    lines.append("| 指标 | A | B | Δ |")
    lines.append("|------|---|---|---|")

    l2 = diff.get("l2", {})
    l2_labels = {
        "section_hit_rate": "Section 命中率",
        "section_misentry_rate": "Section 误入率",
        "section_miss_rate": "Section 漏出率",
        "profile_quality": "画像质量综合分",
    }

    for field, label in l2_labels.items():
        d = l2.get(field, {})
        a_val = d.get("a", "-")
        b_val = d.get("b", "-")
        delta = d.get("delta", "-")
        sign = "+" if isinstance(delta, (int, float)) and delta > 0 else ""
        delta_str = f"{sign}{delta}" if isinstance(delta, (int, float)) else str(delta)
        lines.append(f"| {label} | {a_val} | {b_val} | {delta_str} |")

    lines.append("")
    lines.append("---")
    lines.append("*注：Δ = B - A，正值表示 B 优于 A。不做自动裁决，结论留人工判断。*")

    return "\n".join(lines)
