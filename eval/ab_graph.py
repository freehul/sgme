"""eval/ab_graph.py：T-134 图召回 v1 A/B（复用 T-129 副本/GT 基建）。

目的：验证「BM25 + memory_edges 1-hop 邻居增量候选」在 T-129 内部基线上的
效果——recall@k **不劣化**（直接命中不被挤掉）且 **multi-hop 类提升**
（scene 簇成员 / 归档后继经图路召回）。

双臂口径：纯 BM25（vector.enabled=False，0-token，与 T-129 基线同口径），
仅 `search.graph.enabled` 不同，确保差异只来自图召回。副本上 memory_edges
为空时自动 backfill（零 token）——必须指向**副本**（可写），禁止指向生产库。

用法：
    python -m eval.ab_graph --output eval/results/ab_graph            # 合成 mini 副本
    python -m eval.ab_graph --replica <memory.db副本> --output <dir>  # 真实副本
    python -m eval.ab_graph --weight 0.5 --sample 200 --multi-hop-ratio 0.3
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sgme.data import db as db_mod
from sgme.data import edge_dao
from sgme.data import search as search_mod

from eval import metrics as eval_metrics
from eval.realdb import RealDbGt, build_realdb_gt, make_mini_replica, open_replica

DEFAULT_WEIGHT = 1.0  # search.graph.weight 默认（A/B 实测最优，与 config 默认一致）


def ensure_edges(mem_conn) -> int:
    """副本上确保 memory_edges 就绪：为空则 backfill（零 token）。返回边总数。"""
    cnt = mem_conn.execute("SELECT COUNT(*) AS c FROM memory_edges").fetchone()["c"]
    if cnt == 0:
        edge_dao.backfill_system_edges(mem_conn)
        cnt = mem_conn.execute("SELECT COUNT(*) AS c FROM memory_edges").fetchone()["c"]
    return cnt


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(int(len(s) * 0.95), len(s) - 1)]


def _run_arm(mem_conn, gt: RealDbGt, *, graph_enabled: bool, graph_weight: float,
             limit: int = 10, fill_only: bool = False,
             exclude_relations: list[str] | None = None,
             relation_weights: dict[str, float] | None = None) -> dict:
    """单臂：对每条 GT 跑 search_memories（BM25 + 可选图路），累计 recall@k / P95 延迟。

    逐条记录 hop kind（single / scene / supersession），供按子集聚合。
    T-137 v2：exclude_relations / relation_weights 透传 search.graph（v1 臂传
    []/None 即全边无差别——与 T-134 行为逐字节等价）。
    """
    cfg = {
        "search": {
            "vector": {"enabled": False},
            "graph": {
                "enabled": graph_enabled, "weight": graph_weight, "fill_only": fill_only,
                "exclude_relations": exclude_relations,
                "relation_weights": relation_weights,
            },
        }
    }
    per_query_recall: list = []
    per_query_p1: list[float] = []
    hop_kinds: list[str] = []
    latencies: list[float] = []
    for item in gt.items:
        t0 = time.perf_counter()
        res = search_mod.search_memories(
            mem_conn, None, query=item.query, limit=limit,
            include_sources=False, cfg=cfg,
        )
        latencies.append((time.perf_counter() - t0) * 1000)
        predicted = [r["memory_id"] for r in res]
        rel = item.relevant_ids
        per_query_recall.append(eval_metrics.compute_recall_at_k(predicted, rel, ks=(1, 3, 5, 10)))
        per_query_p1.append(1.0 if predicted and predicted[0] in rel else 0.0)
        hop_kinds.append(getattr(item, "hop_type", "single") or "single")

    def _agg(kinds: list[str]) -> dict | None:
        idx = [i for i, k in enumerate(hop_kinds) if k in kinds]
        if not idx:
            return None
        return eval_metrics.aggregate_recall_at_k(
            [per_query_recall[i] for i in idx], ks=(1, 3, 5, 10)).as_dict()

    return {
        "recall_at_k": _agg(["single", "scene", "supersession"]) or {},
        "recall_scene": _agg(["scene"]),
        "recall_supersession": _agg(["supersession"]),
        "recall_single": _agg(["single"]),
        "count_single": sum(1 for k in hop_kinds if k == "single"),
        "count_scene": sum(1 for k in hop_kinds if k == "scene"),
        "count_supersession": sum(1 for k in hop_kinds if k == "supersession"),
        "precision_at_1": round(sum(per_query_p1) / len(per_query_p1), 4) if per_query_p1 else 0.0,
        "p95_latency_ms": round(_p95(latencies), 2),
        "query_count": len(gt.items),
    }


def _subset_gt(gt: RealDbGt, kind: str | None) -> RealDbGt:
    """按 hop kind 过滤 GT（scene / supersession / None=全量）。"""
    if not kind or kind == "all":
        return gt
    return RealDbGt(items=[it for it in gt.items if it.hop_type == kind], source=gt.source)


def run_graph_ab(
    mem_conn,
    *,
    sample_n: int = 200,
    multi_hop_ratio: float = 0.3,
    seed: int = 0,
    limit: int = 10,
    graph_weight: float = DEFAULT_WEIGHT,
    multi_hop_kind: str | None = None,
    fill_only: bool = False,
) -> dict:
    """跑图开/关双臂 A/B，返回对比结果 dict。

    ``multi_hop_kind``：聚焦多跳子集（"scene" / "supersession" / None=全量）。
    scene 型是图召回（1-hop 共现联想）的直接受益对象；supersession 型相关集为
    live 后继，与种子无 1-hop 连通，图路按构造帮不上——分开报避免误判。
    ``fill_only``：fill-only 语义（图候选只填空位、不干预直接命中）。
    """
    edges = ensure_edges(mem_conn)
    gt = build_realdb_gt(
        mem_conn,
        sample_n=sample_n,
        multi_hop_ratio=multi_hop_ratio,
        seed=seed,
        query_style="content",
    )
    gt = _subset_gt(gt, multi_hop_kind)
    off = _run_arm(mem_conn, gt, graph_enabled=False, graph_weight=graph_weight,
                   limit=limit, fill_only=fill_only)
    on = _run_arm(mem_conn, gt, graph_enabled=True, graph_weight=graph_weight,
                  limit=limit, fill_only=fill_only)
    return {
        "query_count": len(gt.items),
        "gt_source": gt.source,
        "edges_total": edges,
        "graph_weight": graph_weight,
        "multi_hop_kind": multi_hop_kind or "all",
        "fill_only": fill_only,
        "graph_off": off,
        "graph_on": on,
    }


def _fmt_recall(rec: dict | None) -> str:
    if not rec:
        return "-"
    return " / ".join(f"{rec.get(f'recall@{k}', 0.0):.4f}" for k in (1, 3, 5, 10))


def inject_semantic_edges(mem_conn, *, n_similar: int = 300, n_contradicts: int = 30,
                          seed: int = 0) -> dict:
    """T-137：副本注入合成语义边（模拟 L1.5 提炼产物，source='l1_conflict'）。

    生产语义边随提炼运行时产生（目前生产无），A/B 用合成边验证图召回 v2 对
    语义边的处理机制：①similar 边纳入联想（weight=LLM 置信 0.6-0.95）②contradicts
    否定边被 v2 过滤。幂等：先 DELETE source='l1_conflict' 再重插。
    """
    import random
    rng = random.Random(seed)
    mems = [r["memory_id"] for r in mem_conn.execute(
        "SELECT memory_id FROM memories WHERE status='active' ORDER BY memory_id").fetchall()]
    if len(mems) < 2:
        return {"similar": 0, "contradicts": 0, "total": 0}
    edge_dao.delete_edges_by_source(mem_conn, "l1_conflict")

    def _rand_pair(exclude: set[tuple[str, str]]) -> tuple[str, str] | None:
        for _ in range(200):
            a, b = rng.sample(mems, 2)
            key = (a, b) if a < b else (b, a)
            if key not in exclude:
                exclude.add(key)
                return key
        return None

    seen: set[tuple[str, str]] = set()
    n_sim = n_cont = 0
    for _ in range(n_similar):
        pair = _rand_pair(seen)
        if not pair:
            break
        edge_dao.create_edge(mem_conn, pair[0], pair[1], "similar",
                             weight=round(rng.uniform(0.6, 0.95), 3), source="l1_conflict")
        n_sim += 1
    for _ in range(n_contradicts):
        pair = _rand_pair(seen)
        if not pair:
            break
        edge_dao.create_edge(mem_conn, pair[0], pair[1], "contradicts",
                             weight=round(rng.uniform(0.6, 0.9), 3), source="l1_conflict")
        n_cont += 1
    mem_conn.commit()
    return {"similar": n_sim, "contradicts": n_cont, "total": n_sim + n_cont}


def run_graph_ab_v2(
    mem_conn,
    *,
    sample_n: int = 200,
    multi_hop_ratio: float = 0.3,
    seed: int = 0,
    limit: int = 10,
    graph_weight: float = DEFAULT_WEIGHT,
    multi_hop_kind: str | None = None,
    fill_only: bool = True,
    n_semantic_similar: int = 300,
    n_semantic_contradicts: int = 30,
) -> dict:
    """T-137：图召回 v1 vs v2 双臂 A/B（同图开基线，仅关系处理不同）。

    - v1 臂：exclude_relations=[] + relation_weights=None（全边无差别纳入——
      含 contradicts 否定边；与 T-134 行为逐字节等价）
    - v2 臂：exclude_relations=["contradicts"] + relation_weights={"belongs_to": 0.3}
      （否定边过滤 + 共现边尺度压缩，语义边 similar/causes 1.0）

    验收（T-137）：「v2 相比 v1 在 T-129 基线上 multi-hop 类（scene/supersession）
    再提升或持平」——对比 recall_scene / recall_supersession / 全量。
    """
    edges = ensure_edges(mem_conn)
    sem = inject_semantic_edges(
        mem_conn, n_similar=n_semantic_similar, n_contradicts=n_semantic_contradicts, seed=seed,
    )
    gt = build_realdb_gt(
        mem_conn, sample_n=sample_n, multi_hop_ratio=multi_hop_ratio,
        seed=seed, query_style="content",
    )
    gt = _subset_gt(gt, multi_hop_kind)
    v1 = _run_arm(mem_conn, gt, graph_enabled=True, graph_weight=graph_weight,
                  limit=limit, fill_only=fill_only,
                  exclude_relations=[], relation_weights=None)
    v2 = _run_arm(mem_conn, gt, graph_enabled=True, graph_weight=graph_weight,
                  limit=limit, fill_only=fill_only,
                  exclude_relations=["contradicts"], relation_weights={"belongs_to": 0.3})
    return {
        "query_count": len(gt.items),
        "gt_source": gt.source,
        "edges_total": edges,
        "semantic_edges_injected": sem,
        "graph_weight": graph_weight,
        "multi_hop_kind": multi_hop_kind or "all",
        "fill_only": fill_only,
        "v1": v1,
        "v2": v2,
    }


def _report_md_v2(result: dict, corpus: dict) -> str:
    v1, v2 = result["v1"], result["v2"]
    sem = result["semantic_edges_injected"]
    lines = [
        "# T-137 图召回 v2（纳入语义边）A/B 报告",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 语料规模：{corpus.get('size')} 记忆 / 结构边 {result['edges_total']} / 合成语义边 {sem['total']}（similar {sem['similar']} + contradicts {sem['contradicts']}）",
        f"- GT：{result['query_count']} 条（single {v2['count_single']} / scene {v2['count_scene']} / supersession {v2['count_supersession']}，"
        f"source={result['gt_source']}，multi_hop_kind={result['multi_hop_kind']}）",
        f"- 双臂口径：同图开（weight={result['graph_weight']}，fill_only={result['fill_only']}），仅关系处理不同",
        f"  - v1：全边无差别（含 contradicts 否定边）",
        f"  - v2：exclude contradicts + belongs_to×0.3（语义边 similar/causes 1.0）",
        "",
        "## recall@k（k=1/3/5/10：v2 vs v1）",
        "",
        "| 子集 | v1（全边） | v2（过滤+加权） |",
        "|---|---|---|",
        f"| 全量 | {_fmt_recall(v1['recall_at_k'])} | {_fmt_recall(v2['recall_at_k'])} |",
        f"| scene（共现联想） | {_fmt_recall(v1['recall_scene'])} | {_fmt_recall(v2['recall_scene'])} |",
        f"| supersession（live 后继） | {_fmt_recall(v1['recall_supersession'])} | {_fmt_recall(v2['recall_supersession'])} |",
        f"| single | {_fmt_recall(v1['recall_single'])} | {_fmt_recall(v2['recall_single'])} |",
        "",
        "## 延迟与精度",
        "",
        f"- P95 检索延迟：v1 **{v1['p95_latency_ms']}ms** / v2 {v2['p95_latency_ms']}ms（差 {v2['p95_latency_ms'] - v1['p95_latency_ms']:+.1f}ms）",
        f"- precision@1：v1 {v1['precision_at_1']} / v2 {v2['precision_at_1']}",
        "",
        "## 结论",
        "",
    ]
    r5_v1 = v1["recall_at_k"].get("recall@5", 0.0)
    r5_v2 = v2["recall_at_k"].get("recall@5", 0.0)
    s5_v1 = (v1["recall_scene"] or {}).get("recall@5", 0.0)
    s5_v2 = (v2["recall_scene"] or {}).get("recall@5", 0.0)
    ok_not_degrade = r5_v2 >= r5_v1 - 1e-9
    ok_scene = s5_v2 >= s5_v1 - 1e-9
    if ok_not_degrade and ok_scene:
        lines.append(
            "✅ v2 相比 v1：全量 recall@5 不劣化、scene 类提升或持平 —— 符合 T-137"
            "验收口径（multi-hop 类再提升或持平）。"
        )
    else:
        lines.append(
            "❌ v2 相比 v1 劣化（recall@5 下降或 scene 类下降）—— 需复查"
            "exclude/weight 参数后重跑。"
        )
    return "\n".join(lines)


def _report_md(result: dict, corpus: dict) -> str:
    off, on = result["graph_off"], result["graph_on"]
    lines = [
        "# T-134 图召回 v1 A/B 报告",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 语料规模：{corpus.get('size')} 记忆 / 边总量 {result['edges_total']}",
        f"- GT：{result['query_count']} 条（single {on['count_single']} / scene {on['count_scene']} / supersession {on['count_supersession']}，"
        f"source={result['gt_source']}，multi_hop_kind={result['multi_hop_kind']}）",
        f"- 双臂口径：纯 BM25（vector 关闭），仅 `search.graph.enabled` 不同；graph weight={result['graph_weight']}，fill_only={result['fill_only']}",
        "",
        "## recall@k（k=1/3/5/10：图开 vs 图关）",
        "",
        "| 子集 | 图关 | 图开 |",
        "|---|---|---|",
        f"| 全量 | {_fmt_recall(off['recall_at_k'])} | {_fmt_recall(on['recall_at_k'])} |",
        f"| scene（共现联想，图路直接受益） | {_fmt_recall(off['recall_scene'])} | {_fmt_recall(on['recall_scene'])} |",
        f"| supersession（live 后继，无 1-hop 连通） | {_fmt_recall(off['recall_supersession'])} | {_fmt_recall(on['recall_supersession'])} |",
        f"| single | {_fmt_recall(off['recall_single'])} | {_fmt_recall(on['recall_single'])} |",
        "",
        "## 延迟与精度",
        "",
        f"- P95 检索延迟：图关 **{off['p95_latency_ms']}ms** / 图开 {on['p95_latency_ms']}ms（增幅 {on['p95_latency_ms'] - off['p95_latency_ms']:+.1f}ms，验收 <100ms）",
        f"- precision@1：图关 {off['precision_at_1']} / 图开 {on['precision_at_1']}",
        "",
        "## 结论",
        "",
    ]
    r5_off = off["recall_at_k"].get("recall@5", 0.0)
    r5_on = on["recall_at_k"].get("recall@5", 0.0)
    s5_off = (off["recall_scene"] or {}).get("recall@5", 0.0)
    s5_on = (on["recall_scene"] or {}).get("recall@5", 0.0)
    lat_delta = on["p95_latency_ms"] - off["p95_latency_ms"]
    ok_not_degrade = r5_on >= r5_off - 1e-9
    ok_scene = s5_on > s5_off + 1e-9
    ok_latency = lat_delta < 100
    if ok_not_degrade and ok_scene and ok_latency:
        lines.append(
            "✅ 图召回开启后：全量 recall@5 不劣化、scene 共现类 recall@5 提升、"
            "P95 延迟增幅 <100ms —— 符合 T-134 验收口径。"
        )
    elif ok_not_degrade and ok_latency and not ok_scene:
        lines.append(
            "⚠️ recall@5 不劣化且延迟达标，但 scene 共现类未见提升 —— 触发 T-134"
            "止损判定：无增益则重评估 T-135/T-136 投入。"
        )
    else:
        lines.append(
            "❌ 不满足验收（recall 劣化或延迟超标）—— 需复查图路实现/参数后重跑。"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="T-134/T-137 图召回 A/B（on-off / v1-vs-v2）")
    ap.add_argument("--replica", default=None, help="真实 memory.db 副本路径（可写副本，非生产库）")
    ap.add_argument("--output", default="eval/results/ab_graph", help="报告输出目录")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--multi-hop-ratio", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--weight", type=float, default=DEFAULT_WEIGHT)
    ap.add_argument("--multi-hop-kind", choices=["all", "scene", "supersession"],
                    default="all", help="聚焦多跳子集（scene=共现联想，图路直接受益）")
    ap.add_argument("--fill-only", action="store_true",
                    help="fill-only 语义：图候选只填空位、不干预直接命中")
    ap.add_argument("--mode", choices=["on-off", "v1-vs-v2"], default="on-off",
                    help="on-off=图开/关双臂（T-134）；v1-vs-v2=全边 vs 过滤+加权（T-137，注入合成语义边）")
    ap.add_argument("--n-similar", type=int, default=300, help="T-137：注入合成 similar 边数")
    ap.add_argument("--n-contradicts", type=int, default=30, help="T-137：注入合成 contradicts 边数")
    args = ap.parse_args()

    if args.replica:
        mem_conn = open_replica(Path(args.replica), readonly=False)
        replica_is_tmp = False
    else:
        tmp = Path(tempfile.mkdtemp(prefix="ab_graph_", dir="eval/_ab_tmp"))
        rep = make_mini_replica(tmp, n=12, seed=args.seed)
        mem_conn = open_replica(rep, readonly=False)
        replica_is_tmp = True

    try:
        from eval.realdb import replica_corpus_stats

        corpus = replica_corpus_stats(mem_conn)
        kind = None if args.multi_hop_kind == "all" else args.multi_hop_kind
        if args.mode == "v1-vs-v2":
            result = run_graph_ab_v2(
                mem_conn,
                sample_n=args.sample,
                multi_hop_ratio=args.multi_hop_ratio,
                seed=args.seed,
                limit=args.limit,
                graph_weight=args.weight,
                multi_hop_kind=kind,
                fill_only=args.fill_only,
                n_semantic_similar=args.n_similar,
                n_semantic_contradicts=args.n_contradicts,
            )
            report_fn = _report_md_v2
        else:
            result = run_graph_ab(
                mem_conn,
                sample_n=args.sample,
                multi_hop_ratio=args.multi_hop_ratio,
                seed=args.seed,
                limit=args.limit,
                graph_weight=args.weight,
                multi_hop_kind=kind,
                fill_only=args.fill_only,
            )
            report_fn = _report_md
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ab_report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out / "ab_report.md").write_text(report_fn(result, corpus), encoding="utf-8")
        print(report_fn(result, corpus))
        print(f"\n报告已落盘：{out / 'ab_report.json'}")
    finally:
        mem_conn.close()


if __name__ == "__main__":
    main()
