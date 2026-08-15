#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/dedup_memories.py：记忆 content 重复清理（2026-08-16 T-69）。

背景：08-06 及更早的历史数据（L1.5 冲突裁决上线前）存在 content 重复
（全库 11634 条中 10 组重复，n=5 一组/n=4 一组/n=3 八组），同 source_ref
挂 271 条记忆（无 source_ref 锚点时代的遗留）。

策略：
- 只处理 status='active' 且 content 完全重复的记忆
- 每组保留 updated_at 最新一条 active，其余归档（memory_archive，原件不删）
- 归档复用 memory_dao.archive_memory（复制到 archive + 清理 tags/sources/vectors + 删除原行）
- 事务内逐条执行，单条失败不中断（继续处理），结束打印统计

用法：
  python scripts/dedup_memories.py [--data-dir DATA_DIR] [--apply]

安全：
- 默认 dry-run（只统计不归档）；加 --apply 才真正执行
- 执行前自动备份 memory.db 到 data/backups/pre_dedup/
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.data import db as db_mod, memory_dao


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆 content 重复清理")
    parser.add_argument("--data-dir", default=None, help="data 目录（默认项目 data/）")
    parser.add_argument("--apply", action="store_true", help="真正执行归档（默认 dry-run）")
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else db_mod.DATA_DIR
    mem_conn, _, _ = db_mod.init_databases(data_dir)

    # 1. 找重复组：status=active 且 content 出现多次
    dups = mem_conn.execute(
        "SELECT content FROM memories WHERE status='active' GROUP BY content HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC"
    ).fetchall()
    total_dup_groups = len(dups)
    total_dup_rows = sum(
        mem_conn.execute("SELECT COUNT(*) FROM memories WHERE status='active' AND content=?", (c,)).fetchone()[0]
        for (c,) in dups
    )
    print(f"[dedup] 重复组: {total_dup_groups}，涉及 active 记忆: {total_dup_rows} 条")

    if not dups:
        print("[dedup] 无重复，无需清理")
        mem_conn.close()
        return 0

    # 2. 备份（执行前）
    if args.apply:
        backup_dir = data_dir / "backups" / "pre_dedup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        src = data_dir / "memory.db"
        dst = backup_dir / f"memory.db.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(src, dst)
        print(f"[dedup] 备份: {dst}")

    archived = 0
    kept = 0
    errors = []
    for (content,) in dups:
        rows = mem_conn.execute(
            "SELECT memory_id FROM memories WHERE status='active' AND content=? ORDER BY updated_at DESC, created_at DESC",
            (content,),
        ).fetchall()
        # 最新一条保留，其余归档
        keeper = rows[0]["memory_id"]
        kept += 1
        for r in rows[1:]:
            try:
                if args.apply:
                    memory_dao.archive_memory(mem_conn, r["memory_id"], superseded_by=keeper)
                archived += 1
            except Exception as e:
                errors.append(f"{r['memory_id'][:8]}: {e}")

    if args.apply:
        mem_conn.commit()
    print(f"[dedup] 保留 {kept} 条，归档 {archived} 条" + ("（dry-run，未实际执行）" if not args.apply else ""))
    if errors:
        print(f"[dedup] 失败 {len(errors)} 条:")
        for e in errors[:10]:
            print(f"  {e}")
        return 1
    mem_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
