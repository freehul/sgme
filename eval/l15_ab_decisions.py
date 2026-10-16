"""L1.5 决策级 A/B 对照器（ST-58/T-296，G3-c 门禁）。

解决什么问题：
既有评测框架只度量**检索层** recall@k，而 L1.5 的作用是**裁决**
（store / skip / update / merge）——裁决错误（该 update 却 store、重复残留）
不会显著改变 recall（DS 审查 K7 + 实测 N6）。本脚本提供**决策级**对照能力，
作为批次 2（B2 候选数收紧）/ 批次 3（B1' 元数据裁剪）/ 批次 4（B3 装箱校准）
的放行门禁。

实验设计（方案 §4.2/§4.3 的落地）：
1. **输入完全确定性**：池与新记忆来自 `eval/l15_decision_corpus.py` 的手写 fixture，
   不经 LLM 抽取。理由：L1 抽取本身有随机性，若两臂各自重跑 L1，差异会被 L1 噪声
   淹没，测不出 L1.5 的差异（这是方法论要点）。
2. **每臂独立库、同一初始池**：逐臂建全新 data 目录并灌入**完全相同**的池
   （固定 memory_id + 固定 updated_at），保证候选池起点一致。
3. **单变量**：每臂只改目标配置（如 prescreen 阈值），其余保持基线。
4. **两个观测量**：
   - **决策一致率**（相对）：逐条比对与基线臂的 `action` 是否相同 → G3-c
   - **决策准确率**（绝对）：对 fixture 人工标注 `expect` 的命中率
5. **自诊断**：报告每批 `candidate_count`，用于判断 prescreen 上限在本语料上
   **是否真的卡住**——池太小时 50/50 与 30/30 无差异，门禁会给出「零 delta」
   的假绿灯，必须能看出来。

用法（需本地 LM Studio 在线，零云依赖、不碰生产库）：

    .venv\\Scripts\\python.exe eval/l15_ab_decisions.py \\
        --fillers 60 \\
        --out eval/results/l15_ab_p1

退出码：0 = 跑完并出报告；1 = 环境/输入不满足。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import l15_decision_corpus as corpus  # noqa: E402
from sgme import config as sgme_config  # noqa: E402
from sgme.data import db as db_mod  # noqa: E402
from sgme.data import memory_dao  # noqa: E402
from sgme.engine import l15 as l15_mod  # noqa: E402
from sgme.prompts import BucketCtx  # noqa: E402

#: 臂定义：名字 → 覆盖项（只写与基线不同的部分，保证「单变量」可读）
ARMS: dict[str, dict] = {
    "baseline": {
        "note": "基线：prescreen 50/50（生产现值）",
        "prescreen": {"vector_top_k": 50, "dimension_top_n": 50},
    },
    "b2_30_30": {
        "note": "批次 2：候选数收紧 30/30",
        "prescreen": {"vector_top_k": 30, "dimension_top_n": 30},
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_refine_node(base_url: str, model: str) -> dict:
    """本地 LM Studio 提炼节点（零云依赖；照 eval/longmemeval_eval._inject_local_refine）。"""
    return {
        "provider": "lmstudio_local",
        "model": model,
        "provider_type": "openai_compat",
        "base_url": base_url,
        "context_window": int(os.environ.get("SGME_REFINE_CTX", "32768")),
        "api_key_env": None,
        "max_tokens": 16384,
    }


def build_cfg(arm: dict, *, embed_base_url: str, embed_model: str,
              refine_base_url: str, refine_model: str, use_vector: bool,
              refine_backend: str = "local") -> dict:
    """基线 cfg + 臂覆盖（单变量：只有臂声明的字段被改）。

    ``refine_backend``：
    - ``local``（默认）：把提炼链换成本地 LM Studio 节点 + rule 兜底，零云依赖。
    - ``cloud``：**保留基线链不动**（生产链 agnes→siliconflow→rule）——用于回答
      「本地 9B 的结论能否代表生产模型」。此时 embed 仍走本地（嵌入与提炼模型无关，
      且零成本、确定性好）。
    """
    cfg = sgme_config.load_config()
    if refine_backend == "cloud":
        # 保留生产链（含其 throttle/timeout 规则），只确保链存在
        if not (cfg.get("llm", {}).get("chains", {}) or {}).get("refinement"):
            raise RuntimeError("基线 cfg 缺少 refinement 链，无法用 cloud 后端")
    else:
        cfg.setdefault("llm", {})["chains"] = {
            "refinement": [_local_refine_node(refine_base_url, refine_model),
                           {"provider": "rule", "rule": "drop_batch"}],
        }
        cfg["llm"]["rules"] = {"timeout_s": 1800, "max_retries": 2,
                               "throttle": {"enabled": False}}
    cfg.setdefault("search", {})["vector"] = {
        "enabled": bool(use_vector),
        "provider": "local",
        "model": embed_model,
        "base_url": embed_base_url,
        "fallbacks": [],
    }
    cfg.setdefault("l15", {}).setdefault("prescreen", {}).update(arm.get("prescreen", {}))
    # 关闭与本次对照无关的旁路（减少噪声：查重隔离、语义边写库）
    for k, v in (arm.get("l15_extra") or {}).items():
        cfg["l15"][k] = v
    return cfg


def seed_pool(conn, pool: list[dict]) -> None:
    """把池灌入全新库（固定 memory_id / updated_at → 跨臂完全一致）。"""
    for m in pool:
        memory_dao.insert_memory(
            conn,
            content=m["content"],
            memory_type=m.get("memory_type", "fact"),
            priority=m.get("priority", 50),
            time_velocity=m.get("time_velocity", "static"),
            ttl_days=m.get("ttl_days"),
            dimension_ids=m["dimension_ids"],
            memory_id=m["memory_id"],
            created_at=corpus._ts(m.get("updated_day", 1)),
            updated_at=corpus._ts(m.get("updated_day", 1)),
        )


def run_arm(name: str, arm: dict, *, work_dir: Path, pool: list[dict],
            batches: list[dict], embed_base_url: str, embed_model: str,
            refine_base_url: str, refine_model: str, use_vector: bool,
            refine_backend: str = "local") -> dict:
    """跑一个臂：独立库 + 固定池 + 逐批 L1.5 裁决。"""
    arm_dir = work_dir / f"arm_{name}"
    if arm_dir.exists():
        shutil.rmtree(arm_dir)
    data_dir = arm_dir / "data"
    data_dir.mkdir(parents=True)

    cfg = build_cfg(arm, embed_base_url=embed_base_url, embed_model=embed_model,
                    refine_base_url=refine_base_url, refine_model=refine_model,
                    use_vector=use_vector, refine_backend=refine_backend)
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(data_dir)
    try:
        # 全新库的 dimensions 表是空的 → 必须先灌注册表（YAML 为种子，DB 为运行时真相），
        # 否则 memory_tags 的 dimension_id 外键约束直接失败（首次跑实测踩到）。
        # 与 app.py 启动同款调用（幂等）。
        memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
        seed_pool(mem_conn, pool)

        results: list[dict] = []
        for b in batches:
            new_mems = [dict(x) for x in b["new_memories"]]
            res = l15_mod.resolve_conflicts(
                new_mems, mem_conn, cfg, top_k=50,
                source_ref=f"fixture:{b['id']}",
                bucket_ctx=BucketCtx(bucket_key=b["id"]),
            )
            results.append({
                "batch_id": b["id"],
                "expect": b["expect"],
                "note": b["note"],
                "error": res.error,
                "fallback": bool(res.error),
                "decisions": res.decisions,
                "counts": {
                    "stored": len(res.stored), "skipped": len(res.skipped),
                    "updated": len(res.updated), "merged": len(res.merged),
                    "archived": len(res.archived),
                },
            })

        # 成本/候选规模（自诊断：判断 prescreen 上限是否真的卡住）
        runs = mem_conn.execute(
            "SELECT stage, provider, candidate_count, prompt_tokens, elapsed_ms, status "
            "FROM refine_runs WHERE stage='l1_conflict' ORDER BY started_at"
        ).fetchall()
        return {
            "arm": name,
            "note": arm.get("note", ""),
            "prescreen": arm.get("prescreen", {}),
            "batches": results,
            "runs": [dict(r) for r in runs],
        }
    finally:
        for c in (mem_conn, session_conn, wiki_conn):
            try:
                c.close()
            except Exception:
                pass


def _decision_map(arm_result: dict) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for b in arm_result["batches"]:
        for d in b["decisions"]:
            out[(b["batch_id"], d["new_memory_index"])] = d["action"]
    return out


def _accuracy(arm_result: dict) -> dict:
    """绝对质量：逐条决策与人工标注 expect 的命中率。"""
    hit = total = 0
    detail = []
    for b in arm_result["batches"]:
        for d in b["decisions"]:
            total += 1
            ok = d["action"] == b["expect"]
            hit += int(ok)
            if not ok:
                detail.append({"batch_id": b["batch_id"], "expect": b["expect"],
                               "got": d["action"], "reason": d.get("reason", "")[:80]})
    return {"hit": hit, "total": total,
            "rate": round(hit / total, 4) if total else None, "mismatches": detail}


def compare(base: dict, arm: dict) -> dict:
    """逐条决策一致率（G3-c）——只比 action；候选集差异单独报（收紧阈值本就该改变候选）。"""
    mb, ma = _decision_map(base), _decision_map(arm)
    keys = sorted(set(mb) | set(ma))
    same = diff = 0
    diffs = []
    for k in keys:
        if mb.get(k) == ma.get(k):
            same += 1
        else:
            diff += 1
            diffs.append({"batch_id": k[0], "index": k[1],
                          "baseline": mb.get(k), "arm": ma.get(k)})
    total = same + diff
    return {
        "baseline_arm": base["arm"], "arm": arm["arm"],
        "total": total, "same": same, "diff": diff,
        "consistency": round(same / total, 4) if total else None,
        "diffs": diffs,
    }


def summarize_costs(arm: dict) -> dict:
    """成本/规模聚合 + **实际 provider 分布**。

    provider 分布是**诚实性断言**：链可能是 agnes → siliconflow → rule 逐级降级，
    若「生产模型对照」实际由 rule 兜底（零 LLM）产出，结论毫无意义。故必须显式记录
    每个 run 实际由谁服务（`refine_runs.provider`）。
    """
    runs = arm["runs"]
    cands = [r["candidate_count"] for r in runs if r.get("candidate_count") is not None]
    prompts = [r["prompt_tokens"] for r in runs if r.get("prompt_tokens") is not None]
    return {
        "l1_conflict_runs": len(runs),
        "candidate_count_avg": round(sum(cands) / len(cands), 1) if cands else None,
        "candidate_count_max": max(cands) if cands else None,
        "prompt_tokens_sum": sum(prompts) if prompts else 0,
        "errors": sum(1 for r in runs if r.get("status") == "error"),
        "providers": dict(Counter(r.get("provider") for r in runs if r.get("provider"))),
    }


def render_markdown(report: dict) -> str:
    L: list[str] = []
    L.append("# L1.5 决策级 A/B 对照报告")
    L.append("")
    L.append(f"> 生成：{report['generated_at']} ｜ 语料：`eval/l15_decision_corpus.py`"
             f"（池 {report['pool_size']} 条 = 手写 {report['pool_base']} + 填充 "
             f"{report['fillers']}）｜ 向量：{'开' if report['use_vector'] else '关'}"
             f"｜ 提炼后端：{(report.get('refine') or {}).get('backend', 'local')}")
    L.append("")
    L.append("## 一、决策准确率（对人工标注 expect 的绝对命中率）")
    L.append("")
    L.append("| 臂 | 命中/总数 | 准确率 | 成本候选均值 | prompt 合计 | L1.5 error | 实际 provider |")
    L.append("|---|---|---|---|---|---|---|")
    for a in report["arms"]:
        acc = a["accuracy"]
        c = a["cost"]
        L.append(f"| {a['arm']} | {acc['hit']}/{acc['total']} | "
                 f"{(acc['rate']*100 if acc['rate'] is not None else 0):.1f}% | "
                 f"{c['candidate_count_avg']} | {c['prompt_tokens_sum']} | {c['errors']} | "
                 f"`{c.get('providers') or {}}` |")
    L.append("")
    L.append("> **provider 是诚实性断言**：链 agnes→siliconflow→rule 会逐级降级，"
             "若结果实际由 `rule`（零 LLM）产出，本轮结论无意义。核对上表 provider 列。")
    L.append("")
    L.append("## 二、决策一致率（vs 基线臂，G3-c 门禁量）")
    L.append("")
    L.append("| 臂 | 一致/总数 | 一致率 | 达标线 0.90 |")
    L.append("|---|---|---|---|")
    for a in report["arms"]:
        cmp_ = a["vs_baseline"]
        if cmp_ is None:
            L.append(f"| {a['arm']}（基线） | — | — | — |")
            continue
        rate = cmp_["consistency"]
        verdict = "✅" if (rate is not None and rate >= 0.90) else "❌"
        L.append(f"| {a['arm']} | {cmp_['same']}/{cmp_['total']} | "
                 f"{(rate*100 if rate is not None else 0):.1f}% | {verdict} |")
    L.append("")
    L.append("## 三、逐条差异（谁被改判了）")
    L.append("")
    for a in report["arms"]:
        cmp_ = a["vs_baseline"]
        if not cmp_ or not cmp_["diffs"]:
            continue
        L.append(f"### {a['arm']} vs 基线")
        L.append("")
        L.append("| 批 | idx | 基线 | 该臂 |")
        L.append("|---|---|---|---|")
        for d in cmp_["diffs"]:
            L.append(f"| {d['batch_id']} | {d['index']} | {d['baseline']} | {d['arm']} |")
        L.append("")
    L.append("## 四、逐条裁决明细（含 expect 对照）")
    L.append("")
    L.append("| 臂 | 批 | idx | expect | 得到 | 一致? | 理由 |")
    L.append("|---|---|---|---|---|---|---|")
    for a in report["arms"]:
        for b in a["batches"]:
            for d in b["decisions"]:
                ok = "✅" if d["action"] == b["expect"] else "❌"
                L.append(f"| {a['arm']} | {b['batch_id']} | {d['new_memory_index']} | "
                         f"{b['expect']} | {d['action']} | {ok} | "
                         f"{(d.get('reason') or '')[:60]} |")
    L.append("")
    L.append("## 五、自诊断：prescreen 上限是否真的卡住")
    L.append("")
    L.append("> 池太小时 50/50 与 30/30 无差异 → 「零 delta」是**假绿灯**。"
             "若各臂 `candidate_count_avg` 明显低于阈值上限，说明本轮语料未让上限生效，"
             "结论不可用作 B2 放行依据（需加大 `--fillers`）。")
    L.append("")
    L.append("| 臂 | 阈值 | 候选均值 | 候选最大 | L1.5 批数 |")
    L.append("|---|---|---|---|---|")
    for a in report["arms"]:
        c = a["cost"]
        ps = a["prescreen"]
        thr = f"{ps.get('vector_top_k', '-')}/{ps.get('dimension_top_n', '-')}"
        L.append(f"| {a['arm']} | {thr} | {c['candidate_count_avg']} | "
                 f"{c['candidate_count_max']} | {c['l1_conflict_runs']} |")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L1.5 决策级 A/B 对照（G3-c 门禁）")
    ap.add_argument("--fillers", type=int, default=60,
                    help="同维度填充记忆条数（使 prescreen 上限真的卡住；默认 60）")
    ap.add_argument("--out", default="eval/results/l15_ab",
                    help="输出目录（报告 JSON + Markdown + 各臂独立库）")
    ap.add_argument("--arms", default="baseline,b2_30_30",
                    help="要跑的臂（逗号分隔，键见 ARMS）")
    ap.add_argument("--no-vector", action="store_true",
                    help="关闭向量预筛（只看维度 Top-N，调试用）")
    ap.add_argument("--refine-backend", choices=("local", "cloud"), default="local",
                    help="提炼后端：local=本地 LM Studio（默认，零云依赖）；"
                         "cloud=基线生产链 agnes→siliconflow→rule（答案能否外推生产）")
    # ⚠️ 端点默认**固定回环**，不读 SGME_EMBED_*/SGME_REFINE_* 环境变量——
    # 首次实跑踩到：宿主环境里残留的跨机端点（另一台机器:8123）把请求指到了不可达地址，
    # 「零云依赖的本机对照」变成了静默走网络。要换机请显式传参（可审计）。
    ap.add_argument("--embed-base-url", default="http://127.0.0.1:8123/v1")
    ap.add_argument("--embed-model", default="text-embedding-bge-m3-legal-euro-r7")
    ap.add_argument("--refine-base-url", default="http://127.0.0.1:8123/v1")
    ap.add_argument("--refine-model", default="qwen3.8-9b-distill-uncensored-heretic")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.arms.split(",") if n.strip()]
    unknown = [n for n in names if n not in ARMS]
    if unknown:
        print(f"[FAIL] 未知臂: {unknown}（可选: {list(ARMS)}）", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pool = corpus.build_pool(args.fillers)

    print(f"语料：池 {len(pool)} 条（手写 {len(corpus.POOL)} + 填充 {args.fillers}）"
          f"／新记忆批 {len(corpus.BATCHES)}")
    print(f"臂：{names}｜向量预筛：{'关' if args.no_vector else '开'}"
          f"（{args.embed_base_url} / {args.embed_model}）")
    if args.refine_backend == "cloud":
        print("提炼：**云端生产链**（agnes → siliconflow → rule；实际 provider 见报告）")
    else:
        print(f"提炼：本地 {args.refine_base_url} / {args.refine_model}")

    arms_raw = []
    for n in names:
        print(f"\n--- 跑臂 {n}（{ARMS[n].get('note','')}）---")
        r = run_arm(n, ARMS[n], work_dir=out_dir, pool=pool, batches=corpus.BATCHES,
                    embed_base_url=args.embed_base_url, embed_model=args.embed_model,
                    refine_base_url=args.refine_base_url, refine_model=args.refine_model,
                    use_vector=not args.no_vector, refine_backend=args.refine_backend)
        arms_raw.append(r)
        for b in r["batches"]:
            print(f"  [{b['batch_id']}] expect={b['expect']} "
                  f"got={[d['action'] for d in b['decisions']]} "
                  f"err={b['error']}")

    base = arms_raw[0]
    arms_out = []
    for r in arms_raw:
        arms_out.append({
            "arm": r["arm"], "note": r["note"], "prescreen": r["prescreen"],
            "batches": r["batches"], "cost": summarize_costs(r),
            "accuracy": _accuracy(r),
            "vs_baseline": None if r is base else compare(base, r),
        })

    report = {
        "generated_at": _now(),
        "corpus": "eval/l15_decision_corpus.py",
        "pool_base": len(corpus.POOL), "fillers": args.fillers, "pool_size": len(pool),
        "batches": len(corpus.BATCHES), "use_vector": not args.no_vector,
        "embed": {"base_url": args.embed_base_url, "model": args.embed_model},
        "refine": {"backend": args.refine_backend,
                   "base_url": args.refine_base_url, "model": args.refine_model},
        "arms": arms_out,
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_markdown(report)
    (out_dir / "report.md").write_text(md, encoding="utf-8")

    print("\n" + "=" * 60)
    for a in arms_out:
        acc = a["accuracy"]
        line = (f"{a['arm']:<12} 准确率 {acc['hit']}/{acc['total']}"
                f" ({(acc['rate'] or 0)*100:.1f}%)"
                f" 候选均值 {a['cost']['candidate_count_avg']}")
        cmp_ = a["vs_baseline"]
        if cmp_:
            line += f" 一致率 {(cmp_['consistency'] or 0)*100:.1f}%"
        print(line)
    print(f"报告：{out_dir / 'report.json'} / {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
