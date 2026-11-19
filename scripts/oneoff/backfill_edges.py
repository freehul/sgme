# -*- coding: utf-8 -*-
"""scripts/oneoff/backfill_edges.py：零 token 结构边 backfill（ST-38 T-133，source='system'）。

图纸：`SGME-记忆系统进化方案-v0.2.md` §T2-1a（Phase 0 结构边）。幂等可重跑——
单事务内只清理并重插 supersedes / evolves_from 两类 supersession 边（收敛式），
重跑结果一致；场景共现 belongs_to 历史边与语义边（source='llm'/'cooccur'）不删。

T-427（2026-11-20）：场景层退役，场景共现源已摘除。

用法：
  python scripts/oneoff/backfill_edges.py                          # 生产 DATA_DIR，真实写入
  python scripts/oneoff/backfill_edges.py --data-dir <目录>        # 指定库目录
  python scripts/oneoff/backfill_edges.py --dry-run                # 只统计不写库

输出：JSON 统计（superseded_pairs/supersedes_edges/evolves_from_edges/total）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sgme.data import db, edge_dao  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="T-133 结构边 backfill（source=system）")
    ap.add_argument("--data-dir", default=None,
                    help="memory.db 所在目录（默认 config.DATA_DIR）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    conn = db.connect_memory(args.data_dir)
    try:
        stats = edge_dao.backfill_system_edges(conn, dry_run=args.dry_run)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if not args.dry_run:
            print("EDGE_STATS", json.dumps(edge_dao.edge_stats(conn), ensure_ascii=False))
    finally:
        db.close(conn)


if __name__ == "__main__":
    main()
