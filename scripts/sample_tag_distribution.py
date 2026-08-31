# -*- coding: utf-8 -*-
"""T-131 存量 projects/tasks 标签抽样与分布分析。

用法：
    python scripts/sample_tag_distribution.py --replica <memory.db 副本> \
        --dims projects,tasks --sample 200 --seed 0 --output reports/dist.md

不触生产：只读副本，零写入。输出 markdown 分布报告（内容长度 / 共现有效维度频次 /
最近更新分布 / 样本内容截断），供人工定策略（重新打标 / 升格 project_meta / 软删）。

依赖：仅标准库 + sqlite3。
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


# \u4e2d\u6587\u6807\u7b7e\uff08\u907f\u514d Windows \u5199\u76d8 GBK \u5751\uff0c\u7edf\u4e00\u8f6c\u4e49\uff09
T_TITLE = "\u5b58\u91cf\u6807\u7b7e\u6cbb\u7406\u62bd\u6837\u5206\u5e03\u62a5\u544a"
T_DIM = "\u7ef4\u5ea6"
T_TOTAL = "\u6807\u7b7e\u603b\u6570(active)"
T_SAMPLE = "\u62bd\u6837\u6570"
T_LEN = "\u5185\u5bb9\u957f\u5ea6(\u5b57\u7b26)"
T_COOC = "\u5171\u73b0\u6709\u6548\u7ef4\u5ea6\u9891\u6b21"
T_RECENT = "\u6700\u8fd1\u66f4\u65b0\u5206\u5e03"
T_SAMPLES = "\u6837\u672c\u5185\u5bb9(\u622a\u65ad 80 \u5b57)"
T_VALID = "\u6709\u6548\u7ef4\u5ea6(\u9664 projects/tasks)"
T_NO_TAG = "\u8be5\u7ef4\u5ea6 active \u6807\u7b7e\u4e3a 0\uff0c\u65e0\u6cd5\u62bd\u6837"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_valid_dims(conn: sqlite3.Connection, exclude: set[str]) -> set[str]:
    """db \u4e2d active=1 \u7684\u7ef4\u5ea6\u96c6\uff08\u6392\u9664 projects/tasks\uff09\u3002"""
    rows = conn.execute(
        "SELECT id FROM dimension_registry WHERE active=1"
    ).fetchall()
    return {r[0] for r in rows} - exclude


def active_tagged_ids(conn: sqlite3.Connection, dim: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT m.memory_id FROM memories m "
        "JOIN memory_tags t ON m.memory_id=t.memory_id "
        "WHERE t.dimension_id=? AND m.status='active'",
        (dim,),
    ).fetchall()]


def fetch_memory(conn: sqlite3.Connection, mid: str) -> dict:
    row = conn.execute(
        "SELECT content, updated_at, memory_type FROM memories WHERE memory_id=?",
        (mid,),
    ).fetchone()
    dims = [r[0] for r in conn.execute(
        "SELECT dimension_id FROM memory_tags WHERE memory_id=?", (mid,)
    ).fetchall()]
    if row is None:
        return {"content": "", "updated_at": "", "memory_type": "", "dims": dims}
    return {"content": row[0] or "", "updated_at": row[1] or "",
            "memory_type": row[2] or "", "dims": dims}


def bucket_recent(updated_at: str, now: datetime) -> str:
    if not updated_at:
        return "\u65e0\u65f6\u95f4\u6233"
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return "\u65e0\u65f6\u95f4\u6233"
    days = (now - dt).days
    if days <= 7:
        return "\u22647\u5929"
    if days <= 30:
        return "8-30\u5929"
    if days <= 90:
        return "31-90\u5929"
    return ">90\u5929"


def analyze(conn: sqlite3.Connection, dim: str, n: int, seed: int, valid_dims: set[str]) -> dict:
    ids = active_tagged_ids(conn, dim)
    rnd = random.Random(seed)
    sampled = rnd.sample(ids, min(n, len(ids))) if len(ids) > n else list(ids)

    lengths: list[int] = []
    cooc = Counter()
    recent = Counter()
    samples: list[str] = []
    now = datetime.now(timezone.utc)

    for mid in sampled:
        m = fetch_memory(conn, mid)
        lengths.append(len(m["content"]))
        for d in m["dims"]:
            if d in valid_dims:
                cooc[d] += 1
        recent[bucket_recent(m["updated_at"], now)] += 1
        if len(samples) < 15:
            c = m["content"].replace("\n", " ").strip()
            samples.append(c[:80])

    return {
        "dim": dim,
        "total": len(ids),
        "sampled": len(sampled),
        "lengths": lengths,
        "cooc": cooc,
        "recent": recent,
        "samples": samples,
    }


def _fmt_len_hist(lengths: list[int]) -> str:
    if not lengths:
        return "-"
    buckets = [(0, 20), (21, 50), (51, 100), (101, 200), (201, 500), (501, 10**9)]
    labels = ["0-20", "21-50", "51-100", "101-200", "201-500", ">500"]
    cnt = Counter()
    for L in lengths:
        for (lo, hi), lab in zip(buckets, labels):
            if lo <= L <= hi:
                cnt[lab] += 1
                break
    return " | ".join(f"{lab}:{cnt[lab]}" for lab in labels)


def render_report(results: list[dict], valid_dims: set[str]) -> str:
    valid_str = ", ".join(sorted(valid_dims)) or "\u65e0"
    lines = [f"# {T_TITLE}", "", f"> \u751f\u6210\u65f6\u523b\uff1a{_now_iso()}",
             f"> {T_VALID}\uff1a{valid_str}", ""]
    for r in results:
        lines.append(f"## {T_DIM}: `{r['dim']}`")
        lines.append("")
        lines.append(f"- {T_TOTAL}\uff1a**{r['total']}**")
        lines.append(f"- {T_SAMPLE}\uff1a**{r['sampled']}**")
        lines.append(f"- {T_LEN}\uff1a{_fmt_len_hist(r['lengths'])}")
        lines.append("")
        lines.append(f"### {T_COOC}")
        if r["cooc"]:
            for d, c in r["cooc"].most_common(15):
                lines.append(f"- `{d}`: {c}")
        else:
            lines.append(f"- \u65e0\uff08\u6240\u6709\u6837\u672c\u4ec5\u542b projects/tasks\uff0c\u65e0\u5176\u4ed6\u6709\u6548\u7ef4\u5ea6\uff09")
        lines.append("")
        lines.append(f"### {T_RECENT}")
        for k in ["\u22647\u5929", "8-30\u5929", "31-90\u5929", ">90\u5929", "\u65e0\u65f6\u95f4\u6233"]:
            if r["recent"].get(k):
                lines.append(f"- {k}: {r['recent'][k]}")
        lines.append("")
        lines.append(f"### {T_SAMPLES}")
        for s in r["samples"]:
            lines.append(f"- {s}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=T_TITLE)
    ap.add_argument("--replica", required=True, help="memory.db \u526f\u672c\u8def\u5f84\uff08\u53ea\u8bfb\uff09")
    ap.add_argument("--dims", default="projects,tasks", help="\u9017\u53f7\u5206\u9694\u7ef4\u5ea6")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--output", default="reports/tag_distribution.md")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{Path(args.replica).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    exclude = {d.strip() for d in args.dims.split(",") if d.strip()}
    valid_dims = get_valid_dims(conn, exclude)

    results = []
    for dim in exclude:
        ids = active_tagged_ids(conn, dim)
        if not ids:
            print(f"[{dim}] {T_NO_TAG}")
            results.append({"dim": dim, "total": 0, "sampled": 0,
                            "lengths": [], "cooc": Counter(), "recent": Counter(), "samples": []})
            continue
        results.append(analyze(conn, dim, args.sample, args.seed, valid_dims))

    report = render_report(results, valid_dims)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\u62a5\u544a\u5df2\u5199\u5165: {out}")
    for r in results:
        print(f"  {r['dim']}: total={r['total']} sampled={r['sampled']}")


if __name__ == "__main__":
    main()
