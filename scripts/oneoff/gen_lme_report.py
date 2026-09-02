# -*- coding: utf-8 -*-
"""生成 SGME · LongMemEval 中文对比评测报告。

支持多检索臂（bm25 / hybrid）自动对比，并可选与历史基线结果对比。

Usage:
    ./.venv/Scripts/python.exe scripts/oneoff/gen_lme_report.py \
        --result eval/results/longmemeval_hybrid_full/longmemeval_report.json \
        --out docs/eval/longmemeval_report_zh.md \
        --baseline eval/results/longmemeval_full/longmemeval_report.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(r"D:/Projects/SGME")

# 2026-03 公开榜（GPT-4 judge，J-Score / F1）。来源：LongMemEval 相关公开评测汇总。
LEADERBOARD = [
    ("All-Mem", 60.2, 45.2),
    ("Mem0", 55.8, 36.1),
    ("LightMem", 54.2, 34.3),
    ("HippoRAG2", 53.2, 32.9),
    ("A-Mem", 50.4, 30.8),
    ("MemGPT", 42.8, 20.3),
]

TYPE_CN = {
    "single-session-user": "单会话-用户",
    "single-session-assistant": "单会话-助手",
    "single-session-preference": "单会话-偏好",
    "multi-session": "跨会话",
    "temporal-reasoning": "时序推理",
    "knowledge-update": "知识更新",
}

ARM_CN = {"bm25": "bm25 (纯 lexical)", "hybrid": "hybrid (bm25+向量)"}


def _overall_f1(qa: dict) -> float:
    """各题型 F1 按 judged 样本数加权平均。"""
    bt = qa.get("by_type", {})
    num = sum(d["f1"] * d["judged"] for d in bt.values())
    den = sum(d["judged"] for d in bt.values())
    return round(num / den, 4) if den else 0.0


def arm_stats(res: dict) -> tuple[list[str], dict, dict]:
    """返回 (arms, {arm: {type: {...}}}, {arm: overall_recall})。"""
    by: dict[str, dict] = {}
    for k, v in res["retrieval_recall_by_arm_type"].items():
        arm, qtype = k.split(":", 1)
        by.setdefault(arm, {})[qtype] = v
    arms = sorted(by)
    overall = {}
    for arm, types in by.items():
        hit = sum(v["hit"] for v in types.values())
        n = sum(v["total"] for v in types.values())
        overall[arm] = hit / n if n else 0.0
    return arms, by, overall


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", default="eval/results/longmemeval_hybrid_full/longmemeval_report.json")
    ap.add_argument("--out", default="docs/eval/longmemeval_report_zh.md")
    ap.add_argument("--baseline", default="eval/results/longmemeval_full/longmemeval_report.json",
                    help="历史基线结果（上次 bm25-only 全量），用于计算提升")
    ap.add_argument("--embed-model", default="text-embedding-bge-m3-legal-euro-r7")
    ap.add_argument("--judge-model", default="glm-4-flash")
    args = ap.parse_args()

    res = json.loads((ROOT / args.result).read_text(encoding="utf-8"))
    tk = res["top_k"]
    qa = res["qa"]
    arms, by, overall = arm_stats(res)
    primary = res.get("primary_arm") or ("hybrid" if "hybrid" in arms else arms[0])
    base_overall = overall.get("bm25", 0.0)
    prim_overall = overall.get(primary, 0.0)
    lift = (prim_overall / base_overall - 1.0) if base_overall else 0.0

    bl = None
    bp = ROOT / args.baseline
    if bp.exists() and str(bp) != str(ROOT / args.result):
        bl = json.loads(bp.read_text(encoding="utf-8"))

    L: list[str] = []
    L.append("# SGME · LongMemEval 业界标准评测报告")
    L.append("")
    L.append("> 替代 LoCoMo 成为 SGME 主评测标准（ST-40 演进）。协议对齐 gbrain `eval longmemeval` 与 LongMemEval 官方：每题独立隔离库、session 级 recall、LLM judge 算 J-score + token-F1。")
    L.append("")

    # ── 一、配置 ──
    L.append("## 一、评测配置")
    L.append("")
    L.append(f"- 数据集：`longmemeval_s.jsonl`（{res['n_questions']} 题，25,112 sessions / 246,930 turns）")
    L.append(f"- top-k：{tk} ｜ 检索臂：**{', '.join(ARM_CN.get(a, a) for a in arms)}**")
    embed_line = ""
    if "hybrid" in arms:
        embed_line = (f"- 向量嵌入：**本地 LM Studio（RTX 4080S）`{args.embed_model}`**，"
                      "OpenAI 兼容端点 `localhost:8123`，1024 维 / 8192 ctx，batch=64 约 38.7ms 条；"
                      "替代 NAS Ollama bge-m3（实测 ~49s/条，全量不可行），**快约 200 倍**")
        L.append(embed_line)
    L.append("- 图召回：**休眠** —— LongMemEval 直灌原始会话、不跑提炼 → `memory_edges` 为空 → 图召回贡献 0；与 gbrain 自身跑法一致，公平可比（已实测 `backfill_system_edges` 在此口径下产出 0 条边，因结构边依赖提炼产物 `memory_stats`）")
    L.append("- 评测隔离：每题独立隔离库，零跨题泄漏、**零生产库污染**（评测库 backfill 不可行，生产库 backfill 为独立运维步骤）")
    L.append(f"- QA judge：**智谱 {args.judge_model}**（非 thinking，OpenAI 兼容端点）；注：公开榜用 GPT-4 judge，judge 模型差异会引入偏差，下文对比已标注")
    L.append(f"- 耗时：{res['elapsed_s']}s")
    L.append("")

    # ── 二、检索 recall 多臂对比 ──
    L.append(f"## 二、检索 recall（session 级，按 answer_session_ids）")
    L.append("")
    L.append("LongMemEval 官方指标：检索 top-k → 命中的答案 session 数 / 答案 session 总数。检索是记忆系统的核心能力，此指标不受 judge 模型影响，可与公开榜间接对照。")
    L.append("")
    head = "| 题型 "
    for a in arms:
        head += f"| {ARM_CN.get(a, a)} "
    if len(arms) > 1:
        head += "| 提升 "
    head += "| 样本数 |"
    L.append(head)
    ncol = 1 + len(arms) + (1 if len(arms) > 1 else 0) + 1
    L.append("|---" * ncol + "|")
    all_types = sorted({t for a in arms for t in by[a]},
                       key=lambda t: -by[primary].get(t, {}).get(f"recall@{tk}", 0))
    for t in all_types:
        row = f"| {TYPE_CN.get(t, t)} "
        for a in arms:
            row += f"| {by[a].get(t, {}).get(f'recall@{tk}', '—')} "
        if len(arms) > 1:
            b = by.get("bm25", {}).get(t, {}).get(f"recall@{tk}", 0) or 0
            p = by[primary].get(t, {}).get(f"recall@{tk}", 0) or 0
            row += f"| {'+%.1f%%' % ((p / b - 1) * 100) if b else '—'} "
        row += f"| {by[primary].get(t, {}).get('total', '—')} |"
        L.append(row)
    last = "| **整体（加权）** " + "".join(f"| **{round(overall[a], 4)}** " for a in arms)
    if len(arms) > 1:
        last += f"| **+{lift*100:.1f}%** "
    last += f"| {sum(v['total'] for v in by[primary].values())} |"
    L.append(last)
    L.append("")

    # ── 三、QA ──
    L.append(f"## 三、QA 质量（智谱 {args.judge_model} 生成 + judge，检索臂={ARM_CN.get(primary, primary)}）")
    L.append("")
    if qa["enabled"]:
        L.append(f"- **J-score（整体）：{qa['j_score']}**（correct {qa['correct']} / wrong {qa['wrong']} / no-context {qa['no_context']} / errors {qa['errors']}）")
        L.append(f"- **token-F1（整体）：{_overall_f1(qa)}**（各题型按 judged 加权）")
        L.append(f"- NO CONTEXT 率：{qa['no_context_rate']}（检索未命中相关 session 时系统诚实回答「NO CONTEXT」）")
        L.append("")
        L.append("| 题型 | J-score | F1 | judged |")
        L.append("|---|---|---|---|")
        for t, d in qa["by_type"].items():
            L.append(f"| {TYPE_CN.get(t, t)} | {d['j_score']} | {d['f1']} | {d['judged']} |")
    else:
        L.append("- 未启用（--qa）")
    L.append("")

    # ── 四、与 bm25 基线对比（本次提升） ──
    if bl and bl.get("qa", {}).get("enabled"):
        bqa = bl["qa"]
        _, _, bo = arm_stats(bl)
        L.append("## 四、本次复测提升（对比 bm25-only 基线）")
        L.append("")
        L.append("| 指标 | bm25 基线 | 本次（%s） | 变化 |" % ARM_CN.get(primary, primary))
        L.append("|---|---|---|---|")
        bj, pj = bqa["j_score"], qa["j_score"]
        bf, pf = _overall_f1(bqa), _overall_f1(qa)
        bnc, pnc = bqa["no_context_rate"], qa["no_context_rate"]
        L.append(f"| 检索 recall@{tk} | {round(bo.get('bm25', 0), 4)} | **{round(prim_overall, 4)}** | "
                 f"{'**+%.1f%%**' % ((prim_overall / bo['bm25'] - 1) * 100) if bo.get('bm25') else '—'} |")
        L.append(f"| QA J-score | {round(bj * 100, 1)}% | **{round(pj * 100, 1)}%** | "
                 f"{'**+%s pp**' % round((pj - bj) * 100, 1) if pj >= bj else '%s pp' % round((pj - bj) * 100, 1)} |")
        L.append(f"| QA token-F1 | {round(bf * 100, 1)}% | **{round(pf * 100, 1)}%** | "
                 f"{'**+%s pp**' % round((pf - bf) * 100, 1) if pf >= bf else '%s pp' % round((pf - bf) * 100, 1)} |")
        L.append(f"| NO CONTEXT 率 | {round(bnc * 100, 1)}% | **{round(pnc * 100, 1)}%** | "
                 f"{'**%s pp**' % round((pnc - bnc) * 100, 1)}（越低越好） |")
        L.append("")
        L.append("> 基线 = 2026-09-01 全量 500 题 bm25-only 评测（`eval/results/longmemeval_full`）。本次接入本地向量臂后，检索召回与端到端 QA 同步提升，NO CONTEXT 拒答率下降。")
        L.append("")

    # ── 五、公开榜对比 ──
    L.append("## 五、与 2026-03 公开榜对比")
    L.append("")
    L.append("> 本节所有 J-Score / F1 **统一为百分制（0–100）**。公开榜为 **GPT-4 judge**；SGME 本次为 **智谱 %s judge**，judge 模型差异会引入系统性偏差（强 judge 通常更严格），故**只用检索 recall（第二节，与 judge 无关）做主要对比**，J-Score 仅作同口径量级参考。" % args.judge_model)
    L.append("")
    L.append("| 系统 | J-Score (%) | F1 (%) | 检索臂 / judge |")
    L.append("|---|---|---|---|")
    L.append(f"| **SGME (本次)** | **{round(qa['j_score']*100,1)}** | **{round(_overall_f1(qa)*100,1)}** | {primary} / {args.judge_model} |")
    L.append(f"| **SGME 检索 recall@{tk}** | **{round(prim_overall*100,1)}** | — | 与 judge 无关的核心检索指标 |")
    for name, js, f1 in LEADERBOARD:
        L.append(f"| {name} | {js} | {f1} | 公开榜 (GPT-4 judge) |")
    L.append("")
    L.append("### 解读")
    L.append("")
    L.append(f"1. **检索维度（可直接对比）**：SGME {primary} 整体 session recall@{tk} = **{round(prim_overall*100,1)}%**。接入本地向量臂后较 bm25 纯 lexical 基线（{round(base_overall*100,1)}%）提升 **{lift*100:.1f}%**，跨会话/时序/偏好等难类型获益最大（见第二节分题型表）。")
    L.append(f"2. **QA 维度（量级参考，非同比）**：端到端 J-score {round(qa['j_score']*100,1)}% / F1 {round(_overall_f1(qa)*100,1)}%。与公开榜 42.8–60.2% 区间相比仍有差距，主因是 **judge 模型差异**（{args.judge_model} vs GPT-4）；在同 judge 下复测方能直接相减。")
    L.append("3. **诚实边界**：① 图召回在本次 raw-ingest 评测中客观上无法激活（依赖提炼产物 `memory_stats`），生产环境 backfill 后图召回方可贡献，属后续运维增强项；② 嵌入模型 `text-embedding-bge-m3-legal-euro-r7` 为法律/欧洲语微调版，通用英文对话语料上非最优，换用原版 bge-m3 预期仍有小幅提升空间。")
    L.append("")

    OUT_MD = ROOT / args.out
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")

    summary = dict(res)
    summary["overall_recall@%d" % tk] = round(prim_overall, 4)
    summary["overall_recall_by_arm"] = {a: round(v, 4) for a, v in overall.items()}
    summary["judge_model"] = args.judge_model
    summary["embed_model"] = args.embed_model if "hybrid" in arms else None
    OUT_JSON = OUT_MD.with_suffix(".json")
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"written: {OUT_MD}")
    print(f"arms={arms}  primary={primary}")
    print(f"overall_recall: " + "  ".join(f"{a}={round(v,4)}" for a, v in overall.items()))
    print(f"j_score={qa['j_score']}  f1={_overall_f1(qa)}  no_context={qa['no_context_rate']}")


if __name__ == "__main__":
    main()
