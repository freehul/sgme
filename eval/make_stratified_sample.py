# -*- coding: utf-8 -*-
"""从 LongMemEval 数据集按题型分层抽样，产出可复现的子集数据集。

为什么需要：全量 500 题（refined 臂）要 3~4.5 天，先用分层小样拿六题型成绩。
分层必须**固定种子 + 按题型配额**，否则「抽样」不可复现、结论无法对照。

用法：
    python eval/make_stratified_sample.py --dataset <longmemeval_s.jsonl> \
        --out tmp/lme_sample100.json --total 100 --seed 20260912
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


def _largest_remainder(counts: dict[str, int], total: int, floor: int = 1) -> dict[str, int]:
    """按各题型占比分配 total 个名额（最大余数法，且每种至少 floor 个）。"""
    n_all = sum(counts.values())
    raw = {k: max(floor, counts[k] / n_all * total) for k in counts}
    quota = {k: int(v) for k, v in raw.items()}
    # 最大余数补齐差额（可能为负 → 从余数最小者扣）
    order = sorted(raw, key=lambda k: raw[k] - int(raw[k]), reverse=True)
    while sum(quota.values()) < total:
        for k in order:
            if sum(quota.values()) >= total:
                break
            if quota[k] < counts[k]:
                quota[k] += 1
    while sum(quota.values()) > total:
        for k in sorted(order, reverse=True):
            if sum(quota.values()) <= total:
                break
            if quota[k] > floor:
                quota[k] -= 1
    return quota


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--total", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20260912)
    a = ap.parse_args()

    data = json.loads(Path(a.dataset).read_text(encoding="utf-8"))
    by_type: dict[str, list[dict]] = defaultdict(list)
    for q in data:
        by_type[str(q.get("question_type") or "unknown")].append(q)

    counts = {k: len(v) for k, v in by_type.items()}
    quota = _largest_remainder(counts, a.total)
    rng = random.Random(a.seed)

    picked: list[dict] = []
    print(f"数据集 {len(data)} 题 / {len(by_type)} 种题型，抽样 {a.total} 题（seed={a.seed}）")
    for t in sorted(by_type):
        pool = list(by_type[t])
        rng.shuffle(pool)
        take = pool[: quota[t]]
        picked.extend(take)
        n_abs = sum(1 for q in take if str(q.get("question_id", "")).endswith("_abs"))
        print(f"  {t:28s} 全量 {counts[t]:3d} → 抽 {len(take):3d}（含弃答题 {n_abs}）")

    # 保序输出（按题号稳定排序，便于与全量 run 的逐题记录对照）
    picked.sort(key=lambda q: str(q.get("question_id", "")))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(picked, ensure_ascii=False, indent=2), encoding="utf-8")
    suffix = Counter(str(q.get("question_id", "")).rsplit("_", 1)[-1] for q in picked)
    print(f"已写出 {out}（{out.stat().st_size/1e6:.1f} MB）；题型分布={dict(Counter(str(q.get('question_type')) for q in picked))}")
    print(f"题号后缀分布（_abs=弃答题）: {dict(suffix)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
