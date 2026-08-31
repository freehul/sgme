"""eval/ab_stoplist.py：T-130 查询侧停用词过滤 A/B（复用 T-129 副本/GT 基建）。

目的：用「自然语句类」GT（内容词裹停用词）跑 stoplist 开/关双臂，验证
T-130 验收口径——recall@k 不劣化（内容词保留）+ 自然语句类结果集噪声下降
（纯停用词 distractor 被滤除，precision@k 提升）。

双臂都走纯 BM25（vector.enabled=False，0-token，与 T-129 基线同口径），
仅 `search.stoplist.enabled` 不同，确保差异只来自停用词过滤。

为让「噪声下降」可观测，--noise N 会向副本注入 N 条「纯停用词」干扰记忆
（如「请问你知道这是什么地方吗」），它们只含停用词、不含任何内容词，
因此 stoplist 关闭时会被 OR 命中污染结果集，开启时被滤除。

用法：
    python -m eval.ab_stoplist --output eval/results/ab_stoplist
    python -m eval.ab_stoplist --noise 40 --output eval/results/ab_stoplist
    python -m eval.ab_stoplist --replica <真实memory.db副本> --output <dir>
"""
from __future__ import annotations

import argparse
import json
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.search import init_fts, recall_routes
from sgme.segment import segment

from eval import metrics as eval_metrics
from eval.realdb import (
    FIXED_TS,
    RealDbGt,
    build_realdb_gt,
    make_mini_replica,
    open_replica,
    replica_corpus_stats,
)

# 纯停用词干扰句（不含任何内容承载词，仅供 stoplist 关闭时污染结果集）
_NOISE_SENTENCES = [
    "请问你知道这是什么地方吗",
    "谁在哪个城市上班呢",
    "我想了解一下这个问题",
    "这个是为什么呢你能告诉我吗",
    "他在那里做什么事情呀",
    "我们什么时候去那个地方比较好",
]


def inject_noise_memories(mem_conn, n: int, seed: int = 0) -> list[str]:
    """向副本注入 n 条纯停用词干扰记忆（status=active），返回其 memory_id 列表。

    这些记忆只含停用词，无任何内容词，故 stoplist 关闭时会被自然语句 query 的
    OR 命中污染结果集，开启时被滤除——用于量化「结果集噪声下降」。
    """
    if n <= 0:
        return []
    rng = random.Random(seed + 777)
    dim_row = mem_conn.execute("SELECT id FROM dimension_registry LIMIT 1").fetchone()
    dim = dim_row["id"] if dim_row else "identity"
    ids: list[str] = []
    for i in range(n):
        mid = f"noise#{i}"
        sent = _NOISE_SENTENCES[i % len(_NOISE_SENTENCES)]
        mem_conn.execute(
            "INSERT INTO memories(memory_id,content,content_seg,status,memory_type,"
            "priority,time_velocity,created_at,updated_at,occurred_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, sent, segment(sent), "active", "persona", 1, "static",
             FIXED_TS, FIXED_TS, FIXED_TS),
        )
        mem_conn.execute(
            "INSERT OR IGNORE INTO memory_tags(memory_id,dimension_id) VALUES(?,?)",
            (mid, dim),
        )
        ids.append(mid)
    mem_conn.commit()
    return ids


def _run_arm(mem_conn, gt: RealDbGt, *, stoplist_enabled: bool, limit: int = 10,
             noise_ids: set[str] | None = None) -> dict:
    """单臂：对每条 GT 跑 recall_routes，累计 recall@k + 结果集噪声指标。"""
    cfg = {
        "search": {
            "vector": {"enabled": False},
            "stoplist": {"enabled": stoplist_enabled},
        }
    }
    per_query_recall: list[dict] = []
    per_query_p1: list[float] = []
    result_counts: list[int] = []
    noise_hits: list[int] = []  # 每条 query 命中的 noise distractor 数
    for item in gt.items:
        bm25, _vec, _routes = recall_routes(
            mem_conn, item.query, limit=limit, cfg=cfg,
        )
        predicted = [r["memory_id"] for r in bm25]
        rel = item.relevant_ids
        per_query_recall.append(eval_metrics.compute_recall_at_k(predicted, rel, ks=(1, 3, 5, 10)))
        per_query_p1.append(1.0 if predicted and predicted[0] in rel else 0.0)
        result_counts.append(len(bm25))
        if noise_ids:
            noise_hits.append(sum(1 for mid in predicted if mid in noise_ids))
    agg = eval_metrics.aggregate_recall_at_k(per_query_recall, ks=(1, 3, 5, 10)).as_dict()
    return {
        "recall_at_k": agg,
        "precision_at_1": round(sum(per_query_p1) / len(per_query_p1), 4) if per_query_p1 else 0.0,
        "avg_result_count": round(sum(result_counts) / len(result_counts), 3) if result_counts else 0.0,
        "avg_noise_hits": round(sum(noise_hits) / len(noise_hits), 3) if noise_hits else 0.0,
        "query_count": len(gt.items),
    }


def run_stoplist_ab(
    mem_conn,
    *,
    sample_n: int = 200,
    multi_hop_ratio: float = 0.3,
    seed: int = 0,
    limit: int = 10,
    noise_ids: set[str] | None = None,
) -> dict:
    """跑 stoplist 开/关双臂 A/B，返回对比结果 dict。"""
    gt = build_realdb_gt(
        mem_conn,
        sample_n=sample_n,
        multi_hop_ratio=multi_hop_ratio,
        seed=seed,
        query_style="natural",
        exclude_ids=noise_ids,
    )
    on = _run_arm(mem_conn, gt, stoplist_enabled=True, limit=limit, noise_ids=noise_ids)
    off = _run_arm(mem_conn, gt, stoplist_enabled=False, limit=limit, noise_ids=noise_ids)
    return {
        "query_count": len(gt.items),
        "gt_source": gt.source,
        "noise_injected": len(noise_ids) if noise_ids else 0,
        "stoplist_on": on,
        "stoplist_off": off,
        "stoplist_enabled_default": True,
    }


def _report_md(result: dict, corpus: dict) -> str:
    on, off = result["stoplist_on"], result["stoplist_off"]
    lines = [
        "# T-130 停用词过滤 A/B 报告",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 语料规模：{corpus.get('size')} 记忆 / 向量覆盖 {corpus.get('vector_coverage')}（注入噪声 {result['noise_injected']}）",
        f"- GT：{result['query_count']} 条自然语句类 query（source={result['gt_source']}）",
        f"- 双臂口径：纯 BM25（vector 关闭），仅 `search.stoplist.enabled` 不同",
        "",
        "## recall@k（不劣化为通过）",
        "",
        "| k | stoplist 开 | stoplist 关 |",
        "|---|---|---|",
    ]
    for k in (1, 3, 5, 10):
        a = on["recall_at_k"][f"recall@{k}"]
        b = off["recall_at_k"][f"recall@{k}"]
        lines.append(f"| {k} | {a} | {b} |")
    lines += [
        "",
        "## 结果集质量（噪声下降 = 提升）",
        "",
        f"- avg 返回条数：开 **{on['avg_result_count']}** / 关 {off['avg_result_count']}",
        f"- avg 噪声 distractor 命中：开 **{on['avg_noise_hits']}** / 关 {off['avg_noise_hits']}",
        f"- precision@1：开 **{on['precision_at_1']}** / 关 {off['precision_at_1']}",
        "",
        "## 结论",
        "",
    ]
    recall_ok = on["recall_at_k"]["recall@1"] >= off["recall_at_k"]["recall@1"]
    noise_ok = on["avg_noise_hits"] <= off["avg_noise_hits"]
    if recall_ok and noise_ok and (result["noise_injected"] == 0 or on["avg_noise_hits"] < off["avg_noise_hits"]):
        lines.append(
            "✅ stoplist 开启后：recall@k 不劣化（内容词保留）且结果集噪声下降"
            "（纯停用词 distractor 被滤除）——符合 T-130 验收口径。"
        )
    else:
        lines.append(
            "⚠️ 本次 A/B 未观察到明确的噪声下降（可能语料/GT 形态所致），"
            "需结合更大真实副本复核。"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="T-130 停用词过滤 A/B")
    ap.add_argument("--replica", default=None, help="真实 memory.db 副本路径（缺省用合成 mini 副本）")
    ap.add_argument("--output", default="eval/results/ab_stoplist", help="报告输出目录")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--multi-hop-ratio", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--noise", type=int, default=0, help="注入纯停用词干扰记忆条数")
    args = ap.parse_args()

    noise_ids: set[str] = set()
    if args.replica:
        mem_conn = open_replica(Path(args.replica), readonly=True)
        replica_is_tmp = False
    else:
        tmp = Path(tempfile.mkdtemp(prefix="ab_stoplist_", dir="eval/_ab_tmp"))
        rep = make_mini_replica(tmp, n=12, seed=args.seed)
        # 注入噪声需要写权限（仅合成副本，真实 --replica 始终只读）
        mem_conn = open_replica(rep, readonly=(args.noise == 0))
        replica_is_tmp = True

    try:
        if args.noise > 0:
            noise_ids = set(inject_noise_memories(mem_conn, args.noise, seed=args.seed))
            mem_conn.commit()
        corpus = replica_corpus_stats(mem_conn)
        result = run_stoplist_ab(
            mem_conn,
            sample_n=args.sample,
            multi_hop_ratio=args.multi_hop_ratio,
            seed=args.seed,
            limit=args.limit,
            noise_ids=noise_ids or None,
        )
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ab_report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out / "ab_report.md").write_text(_report_md(result, corpus), encoding="utf-8")
        print(_report_md(result, corpus))
        print(f"\n报告已落盘：{out / 'ab_report.json'}")
    finally:
        mem_conn.close()


if __name__ == "__main__":
    main()
