"""eval/loader.py：评测集加载器。

- load_cases: 读 YAML 评测集 → list[EvalCase]
- load_labels: 维度注册表映射（dimension_id → display_name）
- validate_case: 校验单条用例 schema

YAML 格式见 docs/design/SGME-评测框架设计-v0.1.md §3。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from eval.models import (
    EvalCase,
    GtConflictAction,
    GtMemory,
    L15GroundTruth,
    L1GroundTruth,
    L2GroundTruth,
)

logger = logging.getLogger("eval.loader")

# 合法枚举值
VALID_SOURCES = {"real", "synthetic", "edge"}
VALID_DIFFICULTIES = {"easy", "medium", "hard"}
VALID_MEMORY_TYPES = {"persona", "episodic", "instruction"}
VALID_TIME_VELOCITIES = {"static", "dynamic"}
VALID_MODES = {"daily", "coding", "work", "full"}

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_cases(path: str | Path) -> list[EvalCase]:
    """从 YAML 文件加载评测用例列表。

    YAML 格式：
      meta: {version, created_at, total_cases, description}
      cases: [{case_id, source, difficulty, conversation, expected_l1, ...}, ...]

    返回 list[EvalCase]，逐条校验 schema（校验失败抛 ValueError）。
    """
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"评测集文件不存在: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"评测集 YAML 根节点必须是 dict，得到 {type(raw).__name__}")

    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list):
        raise ValueError("评测集 YAML 缺少 cases 数组")

    meta = raw.get("meta", {})
    logger.info("加载评测集: version=%s total=%d desc=%s",
                meta.get("version", "?"), len(cases_raw),
                meta.get("description", "")[:60])

    result: list[EvalCase] = []
    for i, item in enumerate(cases_raw):
        if not isinstance(item, dict):
            logger.warning("跳过非 dict 条目 [%d]: %s", i, type(item).__name__)
            continue
        case = _parse_case(item, i)
        errors = validate_case(case)
        if errors:
            raise ValueError(
                f"用例 {case.case_id or f'#{i}'} schema 校验失败:\n  " +
                "\n  ".join(errors)
            )
        result.append(case)

    if meta.get("total_cases") and len(result) != meta["total_cases"]:
        logger.warning(
            "meta.total_cases=%d 与 cases 数组长度 %d 不一致",
            meta["total_cases"], len(result),
        )

    logger.info("加载完成: %d 条用例", len(result))
    return result


def _parse_case(item: dict, index: int) -> EvalCase:
    """将 YAML dict 解析为 EvalCase 对象。"""
    expected_l1 = _parse_l1_ground_truth(item.get("expected_l1"))
    expected_l15 = _parse_l15_ground_truth(item.get("expected_l15"))
    expected_l2 = _parse_l2_ground_truth(item.get("expected_l2"))

    return EvalCase(
        case_id=str(item.get("case_id", f"eval-{index:03d}")),
        source=str(item.get("source", "synthetic")),
        difficulty=str(item.get("difficulty", "medium")),
        conversation=str(item.get("conversation", "")),
        expected_l1=expected_l1,
        expected_l15=expected_l15,
        expected_l2=expected_l2,
        notes=str(item.get("notes", "")),
    )


def _parse_l1_ground_truth(raw: Any) -> L1GroundTruth:
    """解析 expected_l1 字段。"""
    if not isinstance(raw, dict):
        return L1GroundTruth()

    memories_raw = raw.get("memories")
    if not isinstance(memories_raw, list):
        return L1GroundTruth()

    memories: list[GtMemory] = []
    for m in memories_raw:
        if not isinstance(m, dict):
            continue
        memories.append(GtMemory(
            content=str(m.get("content", "")),
            dimensions=[str(d) for d in m.get("dimensions", [])],
            memory_type=str(m.get("memory_type", "persona")),
            priority=int(m.get("priority", 50)),
            time_velocity=str(m.get("time_velocity", "static")),
            source_message_ids=[str(s) for s in m.get("source_message_ids", [])],
        ))

    return L1GroundTruth(memories=memories)


def _parse_l15_ground_truth(raw: Any) -> L15GroundTruth | None:
    """解析 expected_l15 字段（可选）。"""
    if not isinstance(raw, dict):
        return None

    actions_raw = raw.get("actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        return None

    actions: list[GtConflictAction] = []
    for a in actions_raw:
        if not isinstance(a, dict):
            continue
        actions.append(GtConflictAction(
            new_memory_index=int(a.get("new_memory_index", 0)),
            candidate_ids=[str(c) for c in a.get("candidate_ids", [])],
            action=str(a.get("action", "store")),
            merged_content=a.get("merged_content"),
            reason=str(a.get("reason", "")),
        ))

    return L15GroundTruth(actions=actions) if actions else None


def _parse_l2_ground_truth(raw: Any) -> L2GroundTruth | None:
    """解析 expected_l2 字段（可选）。"""
    if not isinstance(raw, dict):
        return None

    scene_labels = [str(s) for s in raw.get("scene_labels", [])]
    template_section_raw = raw.get("template_section")
    template_section: dict[str, dict[str, str]] = {}

    if isinstance(template_section_raw, dict):
        for mode, mapping in template_section_raw.items():
            if isinstance(mapping, dict):
                template_section[str(mode)] = {
                    str(k): str(v) for k, v in mapping.items()
                }

    return L2GroundTruth(
        scene_labels=scene_labels,
        template_section=template_section,
    )


def validate_case(case: EvalCase) -> list[str]:
    """校验单条评测用例 schema，返回错误列表（空 = 通过）。

    校验规则：
    - case_id 格式 eval-{NNN}
    - source ∈ {real, synthetic, edge}
    - difficulty ∈ {easy, medium, hard}
    - conversation 非空
    - expected_l1.memories 非空
    - 每条 GtMemory.content 非空、dimensions 非空
    - memory_type ∈ {persona, episodic, instruction}
    - priority 0-100
    - time_velocity ∈ {static, dynamic}
    """
    errors: list[str] = []

    if not case.case_id:
        errors.append("case_id 缺失")
    elif not case.case_id.startswith("eval-"):
        errors.append(f"case_id 格式应为 eval-{{NNN}}，实际: {case.case_id}")

    if case.source not in VALID_SOURCES:
        errors.append(f"source 非法值 {case.source!r}，合法: {VALID_SOURCES}")

    if case.difficulty not in VALID_DIFFICULTIES:
        errors.append(f"difficulty 非法值 {case.difficulty!r}，合法: {VALID_DIFFICULTIES}")

    if not case.conversation.strip():
        errors.append("conversation 为空")

    if not case.expected_l1.memories:
        errors.append("expected_l1.memories 为空（至少需要一条 ground truth 记忆）")

    for i, mem in enumerate(case.expected_l1.memories):
        if not mem.content.strip():
            errors.append(f"expected_l1.memories[{i}].content 为空")
        if not mem.dimensions:
            errors.append(f"expected_l1.memories[{i}].dimensions 为空")
        if mem.memory_type not in VALID_MEMORY_TYPES:
            errors.append(
                f"expected_l1.memories[{i}].memory_type 非法: {mem.memory_type!r}"
            )
        if not (0 <= mem.priority <= 100):
            errors.append(
                f"expected_l1.memories[{i}].priority 越界: {mem.priority}（0-100）"
            )
        if mem.time_velocity not in VALID_TIME_VELOCITIES:
            errors.append(
                f"expected_l1.memories[{i}].time_velocity 非法: {mem.time_velocity!r}"
            )

    return errors


def load_labels(registry_path: str | Path | None = None) -> dict[str, str]:
    """加载维度注册表映射 {dimension_id: display_name}。

    默认从 registry/dimensions.yaml 读取。
    """
    if registry_path is None:
        registry_path = PROJECT_ROOT / "registry" / "dimensions.yaml"

    yaml_path = Path(registry_path)
    if not yaml_path.exists():
        logger.warning("维度注册表不存在: %s，返回空映射", yaml_path)
        return {}

    with yaml_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "dimensions" not in raw:
        return {}

    return {
        d["id"]: d.get("display_name", d["id"])
        for d in raw["dimensions"]
        if isinstance(d, dict) and "id" in d
    }


def get_dimension_names(registry_path: str | Path | None = None) -> list[str]:
    """返回注册表中所有维度 id 列表。"""
    labels = load_labels(registry_path)
    return list(labels.keys())
