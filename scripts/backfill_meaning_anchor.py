#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_meaning_anchor.py：D-50 意义锚 M 回填与分布标定（默认 dry-run）。

背景：D-52 的五档处置要用 M 分档；M 为空时处置会跳过（不猜分）。生产首次部署后
需要一次性回填，并按 §9.11.4 的口径出分布（p10/p33/p50/p67/p90/max），确认能分出
高/中/低三档、以及分位阈值落在哪。

用法：

    # 1) 只看当前分布（零写入）
    python scripts/backfill_meaning_anchor.py

    # 2) 回填全部 active 记忆（幂等，可重跑）
    python scripts/backfill_meaning_anchor.py --apply

    # 3) 只补 NULL（梦境每日任务同款）
    python scripts/backfill_meaning_anchor.py --apply --only-null

    # 4) 机器读数（JSON）
    python scripts/backfill_meaning_anchor.py --json

纪律：只写 memories.meaning_anchor 一列（--apply 时），不动 status / lifecycle /
inject_tier；不调 LLM；不改 updated_at（避免污染记录时刻与热度证据）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.data import db  # noqa: E402
from sgme.engine import meaning_anchor  # noqa: E402


def _fmt(dist: dict) -> str:
    if not dist:
        return "（无数据）"
    keys = ("n", "p10", "p33", "p50", "p67", "p90", "min", "max", "mean")
    return "  ".join(f"{k}={dist.get(k)}" for k in keys if k in dist)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-50 意义锚 M 回填与分布（默认 dry-run）")
    ap.add_argument("--data-dir", default=None, help="数据目录（缺省用 config.DATA_DIR）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--only-null", action="store_true", help="只补 meaning_anchor IS NULL 的行")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器读）")
    args = ap.parse_args(argv)

    conn = db.connect_memory(args.data_dir)
    try:
        before = meaning_anchor.distribution(conn)
        total = int(conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0])
        nulls = int(
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE status='active' AND meaning_anchor IS NULL"
            ).fetchone()[0]
        )
        result: dict = {
            "active_total": total,
            "anchor_null": nulls,
            "distribution_before": before,
            "applied": bool(args.apply),
        }
        if args.apply:
            stats = meaning_anchor.recompute(conn, only_null=args.only_null)
            result["updated"] = stats["count"]
            result["distribution_after"] = meaning_anchor.distribution(conn)
        hi, lo = meaning_anchor.tier_thresholds(conn)
        result["tier_thresholds"] = {"high": hi, "low": lo}

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"active={total}  anchor_null={nulls}  applied={args.apply}")
            print("回填前分布: " + _fmt(before))
            if args.apply:
                print(f"本次写入 {result['updated']} 条")
                print("回填后分布: " + _fmt(result.get("distribution_after") or {}))
            print(f"分位阈值: high(p67)={hi}  low(p33)={lo}")
            if not args.apply:
                print("提示：这是 dry-run（零写入）；加 --apply 才回填。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
