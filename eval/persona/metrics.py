# -*- coding: utf-8 -*-
"""eval/persona/metrics.py：D-25 七指标跑分（骨架，不调 LLM）。

输入：
- 黄金集：eval/persona/cases_v001_draft.yaml（loader 校验后的结构）
- 预测：{case_id: {"memories": [...], "causal": [...], "traits": [...]}}
  预测由真实模型在标注集上跑出（或人工填入）；本模块只做确定性计算。

口径（v0，首版从宽跑通，按月收紧；数字必须带标注集版本号+采集日期）：
1. 误存率：tech_counterexample 用例中「预测出非空 memories」的比例（分流护栏）。
2. 部件识别 F1：内容匹配（2-gram Jaccard ≥ 0.5）后的 micro precision/recall/F1。
3. 漏拆率：1 − recall（黄金记忆未被识别出的比例）。
4. 维度一致率：匹配对中 dimensions 集合完全一致的比例。
5. 域映射准确率：匹配对中 main_dimension 一致的比例。
6. 因果误连率：预测 causal 边不在黄金边集合中的比例（集合归一化后）。
7. 刻板推断误报率（代理口径）：匹配对中「预测 confirmed 但黄金 tentative/discussing」
   或「预测 subject=self 但黄金 others」的比例。真值集没有直接的刻板标签，
   该代理口径需在首版基线后人工校准。

附加三条人格记忆口径：
- 偏好召回率：黄金维度含 preferences 的记忆被匹配到的比例。
- 主体归属正确率：匹配对中 subject 一致的比例。
- 软遗忘识别率：黄金含「别再提/忘掉」类记忆时，预测是否产出对应记忆；无样本 → null。

匹配策略：同 case 内按内容 2-gram Jaccard 贪心最优匹配（阈值 0.5），每对唯一。
真实跑分若接 embedding，只需替换 _similarity；其余指标逻辑不变。
"""
from __future__ import annotations

import re
from typing import Any

MATCH_THRESHOLD = 0.5
THRESHOLDS = {
    "false_store_rate": 0.05,
    "component_f1": 0.80,
    "miss_rate": 0.15,
    "dimension_consistency": 0.85,
    "domain_accuracy": 0.85,
    "causal_false_link_rate": 0.10,
    "stereotype_false_positive_rate": 0.10,
    "preference_recall": 0.60,
    "subject_accuracy": 0.85,
    "forget_recall": None,  # 无样本时不判
}

FORGET_RE = re.compile(r"不再主动提及|别再提|忘掉|不要记得|forget", re.IGNORECASE)


def _normalize(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _bigrams(text: Any) -> set[str]:
    s = _normalize(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _similarity(a: Any, b: Any) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _match_memories(gold: list[dict], pred: list[dict]) -> list[tuple[int, int, float]]:
    """贪心最优匹配：返回 [(gold_i, pred_i, similarity)]。"""
    pairs: list[tuple[float, int, int]] = []
    for i, g in enumerate(gold):
        for j, p in enumerate(pred):
            sim = _similarity(g.get("content"), p.get("content"))
            if sim >= MATCH_THRESHOLD:
                pairs.append((sim, i, j))
    pairs.sort(reverse=True)
    used_g: set[int] = set()
    used_p: set[int] = set()
    out: list[tuple[int, int, float]] = []
    for sim, i, j in pairs:
        if i in used_g or j in used_p:
            continue
        used_g.add(i)
        used_p.add(j)
        out.append((i, j, sim))
    return out


def _edge_set(edges: Any) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for e in edges or []:
        if isinstance(e, dict):
            out.add((_normalize(e.get("from")), _normalize(e.get("to"))))
    return out


def evaluate(gold_cases: list[dict], predictions: dict[str, dict]) -> dict[str, Any]:
    """按七指标 + 三条人格口径计算（确定性）。"""
    tech_cases = 0
    tech_false_store = 0
    gold_components = 0
    pred_components = 0
    matched_total = 0
    dim_ok = 0
    domain_ok = 0
    subject_ok = 0
    overclaim = 0
    pref_gold = 0
    pref_hit = 0
    forget_gold = 0
    forget_hit = 0
    causal_pred = 0
    causal_false = 0
    per_case: list[dict] = []

    for case in gold_cases:
        cid = case["id"]
        gold = case["expected"]
        pred = predictions.get(cid) or {}
        g_mems = gold.get("memories") or []
        p_mems = pred.get("memories") or []
        gold_components += len(g_mems)
        pred_components += len(p_mems)

        if case.get("category") == "tech_counterexample":
            tech_cases += 1
            if p_mems:
                tech_false_store += 1

        matches = _match_memories(g_mems, p_mems)
        matched_total += len(matches)
        for gi, pj, _ in matches:
            gm, pm = g_mems[gi], p_mems[pj]
            if sorted(gm.get("dimensions") or []) == sorted(pm.get("dimensions") or []):
                dim_ok += 1
            if gm.get("main_dimension") == pm.get("main_dimension"):
                domain_ok += 1
            if gm.get("subject") == pm.get("subject"):
                subject_ok += 1
            if pm.get("certainty") == "confirmed" and gm.get("certainty") in (
                "tentative", "discussing"
            ):
                overclaim += 1
            if pm.get("subject") == "self" and gm.get("subject") == "others":
                overclaim += 1
            if "preferences" in (gm.get("dimensions") or []):
                pref_gold += 1
                pref_hit += 1
        # 黄金偏好未被匹配的也要计入分母
        for gi, gm in enumerate(g_mems):
            if "preferences" in (gm.get("dimensions") or []) and gi not in {m[0] for m in matches}:
                pref_gold += 1
        # 软遗忘
        for gm in g_mems:
            if FORGET_RE.search(str(gm.get("content") or "")):
                forget_gold += 1
                if any(FORGET_RE.search(str(pm.get("content") or "")) for pm in p_mems):
                    forget_hit += 1

        g_edges = _edge_set(gold.get("causal"))
        p_edges = _edge_set(pred.get("causal"))
        causal_pred += len(p_edges)
        causal_false += len(p_edges - g_edges)

        per_case.append({
            "case_id": cid,
            "gold": len(g_mems),
            "pred": len(p_mems),
            "matched": len(matches),
        })

    precision = (matched_total / pred_components) if pred_components else 0.0
    recall = (matched_total / gold_components) if gold_components else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    metrics = {
        "false_store_rate": (tech_false_store / tech_cases) if tech_cases else 0.0,
        "component_precision": precision,
        "component_recall": recall,
        "component_f1": f1,
        "miss_rate": 1.0 - recall,
        "dimension_consistency": (dim_ok / matched_total) if matched_total else 0.0,
        "domain_accuracy": (domain_ok / matched_total) if matched_total else 0.0,
        "causal_false_link_rate": (
            causal_false / causal_pred if causal_pred else 0.0
        ),
        "stereotype_false_positive_rate": (
            overclaim / matched_total if matched_total else 0.0
        ),
        "preference_recall": (pref_hit / pref_gold) if pref_gold else None,
        "subject_accuracy": (subject_ok / matched_total) if matched_total else 0.0,
        "forget_recall": (forget_hit / forget_gold) if forget_gold else None,
    }

    checks: dict[str, dict[str, Any]] = {}
    for name, target in THRESHOLDS.items():
        value = metrics.get(name)
        if target is None or value is None:
            checks[name] = {"value": value, "target": target, "passed": None}
        elif name in ("false_store_rate", "miss_rate", "causal_false_link_rate",
                      "stereotype_false_positive_rate"):
            checks[name] = {"value": value, "target": target, "passed": value <= target}
        else:
            checks[name] = {"value": value, "target": target, "passed": value >= target}

    return {
        "cases": len(gold_cases),
        "matched": matched_total,
        "gold_components": gold_components,
        "pred_components": pred_components,
        "metrics": metrics,
        "checks": checks,
        "per_case": per_case,
        "notes": [
            "内容匹配 = 2-gram Jaccard ≥ 0.5；接 embedding 时替换 _similarity 即可",
            "刻板推断误报率为代理口径（confirmed 过报 / self 抢 others），需首版后人工校准",
            "软遗忘识别率无样本时为 null，不参与通过判定",
        ],
    }
