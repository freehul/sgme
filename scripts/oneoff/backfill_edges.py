# -*- coding: utf-8 -*-
"""scripts/oneoff/backfill_edges.py：零 token 结构边 backfill（ST-38 T-133，source='system'）。

图纸：`SGME-记忆系统进化方案-v0.2.md` §T2-1a（Phase 0 结构边）。幂等可重跑——
单事务内先 DELETE source='system' 再全量重插（收敛式），重跑结果一致；
语义边（source='llm'/'cooccur'，T2-1b/T2-3 产物）不受影响。

用法：
  python scripts/oneoff/backfill_edges.py                          # 生产 DATA_DIR，真实写入
  python scripts/oneoff/backfill_edges.py --data-dir <目录>        # 指定库目录
  python scripts/oneoff/backfill_edges.py --dry-run                # 只统计不写库
  python scripts/oneoff/backfill_edges.py --top-n 8 --per-scene-top-n 100 --min-weight 1 --cap 200000

输出：JSON 统计（superseded_pairs/supersedes_edges/evolves_from_edges/
      scene_pairs_raw/belongs_to_edges/truncated/total/anomaly）。
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
    ap.add_argument("--top-n", type=int, default=edge_dao.DEFAULT_TOP_N,
                    help=f"每记忆最多邻居数（默认 {edge_dao.DEFAULT_TOP_N}）")
    ap.add_argument("--per-scene-top-n", type=int,
                    default=edge_dao.DEFAULT_PER_SCENE_TOP_N,
                    help=f"每场景参与配对的记忆数上限（默认 {edge_dao.DEFAULT_PER_SCENE_TOP_N}）")
    ap.add_argument("--min-weight", type=int, default=edge_dao.DEFAULT_MIN_WEIGHT,
                    help=f"共现场景数下限（默认 {edge_dao.DEFAULT_MIN_WEIGHT}）")
    ap.add_argument("--cap", type=int, default=edge_dao.DEFAULT_GLOBAL_CAP,
                    help=f"总边量上限（默认 {edge_dao.DEFAULT_GLOBAL_CAP}）")
    args = ap.parse_args()

    conn = db.connect_memory(args.data_dir)
    try:
        stats = edge_dao.backfill_system_edges(
            conn,
            per_scene_top_n=args.per_scene_top_n,
            top_n=args.top_n,
            min_weight=args.min_weight,
            global_cap=args.cap,
            dry_run=args.dry_run,
            publish_anomaly=not args.dry_run,
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if not args.dry_run:
            print("EDGE_STATS", json.dumps(edge_dao.edge_stats(conn), ensure_ascii=False))
    finally:
        db.close(conn)


if __name__ == "__main__":
    main()
