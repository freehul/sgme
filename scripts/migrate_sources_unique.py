#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/migrate_sources_unique.py：memory_sources 加 UNIQUE(memory_id, source_ref)（2026-08-16 T-69）。

背景：历史数据同 source_ref 挂 271 条记忆（08-06 无锚点时代的遗留），
新代码已 INSERT OR IGNORE 防御；本迁移给表加复合主键，防未来再犯。

SQLite 无 ALTER ADD CONSTRAINT → 重建表（12 步标准流程）
- 重命名旧表 → 建新表（带 PRIMARY KEY）→ 拷贝数据 → 删旧表 → 建索引
- 数据去重：拷贝时按 (memory_id, source_ref) 去重，保留 rowid 最小一条
- 可重入：检测已有 PRIMARY KEY 则跳过

用法：
  python scripts/migrate_sources_unique.py [--data-dir DATA_DIR]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.data import db as db_mod


def _has_unique(conn) -> bool:
    """检测 memory_sources 是否已有 (memory_id, source_ref) 主键。"""
    rows = conn.execute("PRAGMA table_info(memory_sources)").fetchall()
    # 检查是否有 pk 列且是复合主键（最后一行 pk>0 表示表级 PK）
    pk_cols = [r[1] for r in rows if r[5] > 0]
    return sorted(pk_cols) == ["memory_id", "source_ref"]


def main() -> int:
    parser = argparse.ArgumentParser(description="memory_sources 加 UNIQUE 约束")
    parser.add_argument("--data-dir", default=None, help="data 目录（默认项目 data/）")
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else db_mod.DATA_DIR
    mem_conn, _, _ = db_mod.init_databases(data_dir)

    if _has_unique(mem_conn):
        print("[migrate] memory_sources 已有复合主键，跳过")
        mem_conn.close()
        return 0

    # 备份
    backup_dir = data_dir / "backups" / "pre_sources_unique"
    backup_dir.mkdir(parents=True, exist_ok=True)
    src = data_dir / "memory.db"
    dst = backup_dir / f"memory.db.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(src, dst)
    print(f"[migrate] 备份: {dst}")

    try:
        mem_conn.execute("BEGIN")
        # 1. 重命名旧表
        mem_conn.execute("ALTER TABLE memory_sources RENAME TO memory_sources_old")
        # 2. 建新表（带复合主键）
        mem_conn.execute("""
            CREATE TABLE memory_sources (
              memory_id TEXT NOT NULL REFERENCES memories(memory_id),
              source_ref TEXT NOT NULL,
              source_type TEXT NOT NULL,
              PRIMARY KEY (memory_id, source_ref))
        """)
        # 3. 拷贝数据（按 (memory_id, source_ref) 去重，保留 rowid 最小一条）
        mem_conn.execute("""
            INSERT INTO memory_sources (memory_id, source_ref, source_type)
            SELECT memory_id, source_ref, source_type
            FROM memory_sources_old
            GROUP BY memory_id, source_ref
            ORDER BY MIN(rowid)
        """)
        # 4. 删旧表
        mem_conn.execute("DROP TABLE memory_sources_old")
        # 5. 重建索引
        mem_conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_mem ON memory_sources(memory_id)")
        mem_conn.commit()
    except Exception as e:
        mem_conn.rollback()
        print(f"[migrate] 失败，已回滚: {e}")
        mem_conn.close()
        return 1

    # 验证
    n = mem_conn.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0]
    print(f"[migrate] 完成，memory_sources 现有 {n} 行")
    assert _has_unique(mem_conn), "约束未生效"
    mem_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
