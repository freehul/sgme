#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验重建 home 的时间轴是否正确（B323 配套验收脚本，2026-11-13）。

用法：
    python scripts/verify_rebuild_timeline.py --home <重建 home> [--threshold 0.1] [--samples 10]

判定：active 记忆里 occurred_at == created_at 的占比 < threshold 即通过（当前生产 0.96）。
退出码：0 通过；1 不通过（占比过高或无样本）；2 参数/文件错误。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="校验重建 home 的 occurred_at 是否来自会话时间")
    p.add_argument("--home", required=True, help="重建 home（含 data/memory.db）")
    p.add_argument("--threshold", type=float, default=0.1, help="occ==created 占比上限（默认 0.1）")
    p.add_argument("--samples", type=int, default=10, help="抽样打印条数")
    args = p.parse_args(argv)

    db = Path(args.home) / "data" / "memory.db"
    if not db.is_file():
        print(f"❌ 未找到 {db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN occurred_at = created_at THEN 1 ELSE 0 END) AS equal_cnt "
            "FROM memories WHERE status = 'active'"
        ).fetchone()
        total = int(row["total"] or 0)
        equal = int(row["equal_cnt"] or 0)
        if not total:
            print("❌ active 记忆为 0，无法验收", file=sys.stderr)
            return 1
        ratio = equal / total
        print(f"active={total} equal_to_created={equal} ratio={ratio:.4f} threshold={args.threshold}")
        rows = conn.execute(
            "SELECT occurred_at, created_at, substr(content,1,40) AS c FROM memories "
            "WHERE status = 'active' ORDER BY created_at LIMIT ?", (args.samples,)
        ).fetchall()
        for r in rows:
            print("  occ=%s created=%s | %s" % (r[0], r[1], r[2]))
    finally:
        conn.close()

    if ratio < args.threshold:
        print("✅ 通过：时间轴不等同于入库时间")
        return 0
    print("❌ 不通过：occurred_at 仍大比例等于 created_at（时间轴退化为提炼时间）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
