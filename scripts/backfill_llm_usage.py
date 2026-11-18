# -*- coding: utf-8 -*-
"""scripts/backfill_llm_usage.py：D-63 成本计量回填入口（T-421，2026-11-18）。

给 ``sgme.engine.cost_meter.backfill_from_refine_runs`` 一个可执行入口：
把 refine_runs 历史批次回填进 ``llm_usage_daily`` 按天账本（幂等，游标表挡重放），
供 ``GET /v1/admin/ops/llm-cost`` 看板取数。

用法
----
    python scripts/backfill_llm_usage.py            # dry-run：只报待回填批次数（零写入）
    python scripts/backfill_llm_usage.py --apply    # 实际回填（幂等，可重复执行）
    python scripts/backfill_llm_usage.py --apply --limit 500   # 限量回填

数据目录默认取 sgme.config（生产由 SGME_HOME 决定），也可 --data-dir 显式指定。
红线：本脚本只写 llm_usage_daily / llm_usage_backfill 两张账本表，refine_runs 只读。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme import config as sgme_config  # noqa: E402
from sgme.data import db as db_mod  # noqa: E402
from sgme.engine import cost_meter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="D-63 LLM 用量回填（dry-run 默认，--apply 才写）")
    ap.add_argument("--apply", action="store_true", help="实际回填（缺省 dry-run 只报告）")
    ap.add_argument("--limit", type=int, default=5000, help="本次最多回填批次数（默认 5000）")
    ap.add_argument("--data-dir", default=None, help="数据库目录（缺省走 sgme.config）")
    a = ap.parse_args()

    d = Path(a.data_dir) if a.data_dir else sgme_config.DATA_DIR
    conn = db_mod.connect_memory(d)
    try:
        if a.apply:
            out = cost_meter.backfill_from_refine_runs(conn, limit=a.limit)
            mode = "APPLY"
        else:
            out = cost_meter.pending_report(conn)
            mode = "DRY-RUN"
    finally:
        db_mod.close(conn)

    print(f"[{mode}] data_dir={d}")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not a.apply:
        print("dry-run 零写入；确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
