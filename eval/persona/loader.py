# -*- coding: utf-8 -*-
"""eval/persona/loader.py：D-25 人格/角色标注集（九类 35 条）加载与校验。

用途：把「静态黄金集」做成可校验的结构——造标、抽查、跑评测前先过关。
- 类别目标（合计 35）= 设计 D-25 覆盖矩阵
- 条目结构 = 原文 + 期望输出（memories 主事件/维度/certainty/subject/route + traits + causal）+ 陷阱提示
- 造标纪律：agent 代造初稿 → 用户抽查 30 分钟；user_checked 未 true 前只算草稿

本模块只做加载/校验/覆盖统计，不调模型、不读写数据库。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# D-25 九类覆盖矩阵（目标合计 35）
CATEGORY_TARGETS: dict[str, int] = {
    "role_interaction": 6,
    "personality_trait": 6,
    "relationship_emotion": 5,
    "health_experience": 4,
    "work_pitfall": 3,
    "tech_counterexample": 3,
    "daily_decision": 3,
    "coarse_date": 3,
    "multi_turn": 2,
}

# 与 sgme/resources/registry/dimensions.yaml 一致的 13 维（health 已并入 status）
DIMENSIONS = {
    "identity", "family", "social", "values", "skills", "tech_stack",
    "preferences", "habits", "environment", "focus", "goals", "status", "ideas",
}
CERTAINTIES = {"confirmed", "tentative", "discussing"}
ROUTES = {"memory", "skill_candidate", "wiki_candidate", "discard"}


class AnnotationError(ValueError):
    """标注集结构非法（缺字段/未知类别/维度越界/分流护栏被破坏）。"""


def load_cases(path: str | Path) -> dict[str, Any]:
    """读 YAML 并返回原始字典（不校验）。"""
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AnnotationError(f"标注集必须是对象: {p}")
    return data


def validate_case(case: dict[str, Any]) -> list[str]:
    """校验单条标注；返回错误列表（空 = 合法）。"""
    errs: list[str] = []
    cid = case.get("id") or "<no-id>"
    if not isinstance(case.get("id"), str) or not case["id"].strip():
        errs.append(f"{cid}: 缺少 id")
    if case.get("category") not in CATEGORY_TARGETS:
        errs.append(f"{cid}: 未知 category {case.get('category')!r}")
    if not isinstance(case.get("conversation"), str) or not case["conversation"].strip():
        errs.append(f"{cid}: 缺少 conversation")
    if not isinstance(case.get("trap"), str) or not case["trap"].strip():
        errs.append(f"{cid}: 缺少 trap（陷阱提示给评测人看）")
    exp = case.get("expected")
    if not isinstance(exp, dict):
        errs.append(f"{cid}: 缺少 expected")
        return errs
    mems = exp.get("memories")
    if not isinstance(mems, list):
        errs.append(f"{cid}: expected.memories 必须是列表")
        return errs
    # 分流护栏：纯技术反例不得产出记忆
    if case.get("category") == "tech_counterexample" and mems:
        errs.append(f"{cid}: 纯技术反例的 expected.memories 必须为空（D-67 分流护栏）")
    for i, m in enumerate(mems):
        if not isinstance(m, dict):
            errs.append(f"{cid}.memories[{i}]: 必须是对象")
            continue
        if not isinstance(m.get("content"), str) or not m["content"].strip():
            errs.append(f"{cid}.memories[{i}]: 缺少 content")
        dims = m.get("dimensions")
        if not isinstance(dims, list) or not dims:
            errs.append(f"{cid}.memories[{i}]: dimensions 必须是非空列表")
        else:
            unknown = [d for d in dims if d not in DIMENSIONS]
            if unknown:
                errs.append(f"{cid}.memories[{i}]: 未知维度 {unknown}")
            main = m.get("main_dimension")
            if main not in dims:
                errs.append(f"{cid}.memories[{i}]: main_dimension 必须在 dimensions 内")
        if m.get("certainty") not in CERTAINTIES:
            errs.append(f"{cid}.memories[{i}]: certainty 非法 {m.get('certainty')!r}")
        if m.get("route") not in ROUTES:
            errs.append(f"{cid}.memories[{i}]: route 非法 {m.get('route')!r}")
        if m.get("subject") not in {"self", "others", "unknown"}:
            errs.append(f"{cid}.memories[{i}]: subject 非法 {m.get('subject')!r}")
    if not isinstance(exp.get("traits", []), list):
        errs.append(f"{cid}: expected.traits 必须是列表")
    if not isinstance(exp.get("causal", []), list):
        errs.append(f"{cid}: expected.causal 必须是列表")
    return errs


def validate_set(data: dict[str, Any]) -> list[str]:
    """校验整份标注集（meta + 逐条）。"""
    errs: list[str] = []
    meta = data.get("meta") or {}
    if not isinstance(meta, dict):
        return ["meta 必须是对象"]
    if meta.get("target_total") != sum(CATEGORY_TARGETS.values()):
        errs.append(
            f"meta.target_total={meta.get('target_total')!r} 与九类目标合计 "
            f"{sum(CATEGORY_TARGETS.values())} 不一致"
        )
    cases = data.get("cases")
    if not isinstance(cases, list):
        return errs + ["cases 必须是列表"]
    ids = [c.get("id") for c in cases if isinstance(c, dict)]
    if len(ids) != len(set(ids)):
        errs.append("存在重复 id")
    for c in cases:
        errs.extend(validate_case(c))
    total = meta.get("total")
    if isinstance(total, int) and total != len(cases):
        errs.append(f"meta.total={total} 与实际条数 {len(cases)} 不一致")
    return errs


def coverage(data: dict[str, Any]) -> dict[str, Any]:
    """按九类统计草稿/缺口（对齐 D-25 目标）。"""
    counts = {k: 0 for k in CATEGORY_TARGETS}
    for c in data.get("cases") or []:
        cat = c.get("category")
        if cat in counts:
            counts[cat] += 1
    matrix = {
        cat: {
            "draft": counts[cat],
            "target": target,
            "remaining": max(0, target - counts[cat]),
        }
        for cat, target in CATEGORY_TARGETS.items()
    }
    return {
        "draft_total": sum(counts.values()),
        "target_total": sum(CATEGORY_TARGETS.values()),
        "remaining_total": sum(v["remaining"] for v in matrix.values()),
        "by_category": matrix,
    }


def build_report(data: dict[str, Any]) -> dict[str, Any]:
    errors = validate_set(data)
    return {
        "version": (data.get("meta") or {}).get("version"),
        "user_checked": (data.get("meta") or {}).get("user_checked", False),
        "errors": errors,
        "coverage": coverage(data),
    }
