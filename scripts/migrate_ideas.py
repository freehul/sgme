#!/usr/bin/env python
"""scripts/migrate_ideas.py：存量创意迁移（memories ideas 标签 → 独立 ideas 表，T-56）。

铁律（A 方案）：
- **原件永不删**——memories 里的旧 ideas 标签记忆保留不动（可溯源），
  仅复制到 ideas 表；origin_memory_id 记录迁移溯源。
- 写前备份 memory.db（data/memory.db.bak-ideas-migrate-<ts>）。
- 幂等：idea_id = 原 memory_id，INSERT OR IGNORE——重复执行零重复。

用法：
  python scripts/migrate_ideas.py            # 迁移（自动备份 + 汇报）
  python scripts/migrate_ideas.py --dry-run  # 只统计不写
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.data import db as db_mod  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="存量创意迁移：memories → ideas 表")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--data-dir", default=None, help="data 目录（缺省用默认）")
    args = ap.parse_args()

    conn = db_mod.connect_memory(args.data_dir)
    try:
        rows = conn.execute(
            """
            SELECT m.memory_id, m.content, m.priority, m.status, m.notes,
                   m.custom_flag, m.reject_reason, m.rejected_at,
                   m.created_at, m.updated_at
            FROM memories m
            JOIN memory_tags t ON m.memory_id = t.memory_id
            WHERE t.dimension_id = 'ideas'
            ORDER BY m.created_at
            """
        ).fetchall()
        existing = set(
            r["idea_id"] for r in conn.execute("SELECT idea_id FROM ideas").fetchall()
        )
        print(f"memories 存量 ideas 标签记忆: {len(rows)} 条；ideas 表现有: {len(existing)} 条")

        if args.dry_run:
            print("dry-run：未写入（见上方统计）")
            return

        if rows:
            backup = Path("data") / f"memory.db.bak-ideas-migrate-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2("data/memory.db", backup)
            print(f"已备份: {backup}")

        inserted = skipped = 0
        for r in rows:
            mid = r["memory_id"]
            if mid in existing:
                skipped += 1
                continue
            # 溯源：memory_sources 首条（对齐创意列表 source_ref 语义）
            src = conn.execute(
                "SELECT source_ref FROM memory_sources WHERE memory_id=? ORDER BY rowid LIMIT 1",
                (mid,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO ideas (idea_id, content, priority, status, notes,
                                   custom_flag, reject_reason, rejected_at,
                                   source_ref, origin_memory_id, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (mid, r["content"], r["priority"], r["status"], r["notes"],
                 r["custom_flag"], r["reject_reason"], r["rejected_at"],
                 src["source_ref"] if src else None, mid,
                 r["created_at"], r["updated_at"]),
            )
            inserted += 1
        conn.commit()
        print(f"迁移完成: 写入 {inserted} 条，跳过（已存在）{skipped} 条；memories 原件保留不动")
        after = conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0]
        print(f"ideas 表现有: {after} 条")
    finally:
        db_mod.close(conn)


if __name__ == "__main__":
    main()
