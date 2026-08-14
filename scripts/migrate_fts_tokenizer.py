"""scripts/migrate_fts_tokenizer.py：手动执行 FTS 分词器迁移（在线幂等）。

用途：已部署库（data/memory.db）升级到「中文检索分词 v0.3」后，手动触发
`content_seg` 回填 + FTS 重建 + `fts_meta.segmenter` marker 写入。
与 `init_fts._ensure_fts_ready` 逻辑同源，本脚本仅提供命令行入口，供不重启
server 的场景使用。

★ 备份纪律（决策 #3）：执行前必须 `cp memory.db memory.db.bak`，
**连同 `-wal`/`-shm` 一并复制**（WAL 模式三件套），避免半写损坏。

用法：
    python -m scripts.migrate_fts_tokenizer [--data-dir data] [--skip-backup]
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("migrate_fts_tokenizer")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _backup_db(db_path: Path) -> Path:
    """复制 memory.db + -wal + -shm 到 `.bak` 三件套（WAL 模式）。"""
    bak = db_path.with_suffix(db_path.suffix + ".bak")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        dst = Path(str(bak) + suffix)
        if src.exists():
            shutil.copy2(src, dst)
            logger.info("备份: %s → %s", src, dst)
        else:
            logger.info("跳过备份（不存在）: %s", src)
    return bak


def main() -> int:
    parser = argparse.ArgumentParser(description="手动执行 FTS 分词器迁移（幂等）")
    parser.add_argument(
        "--data-dir", default="data",
        help="data 目录（默认 data，相对项目根或绝对路径）",
    )
    parser.add_argument(
        "--skip-backup", action="store_true",
        help="跳过备份（不推荐，仅在已手动备份时使用）",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    db_path = data_dir / "memory.db"
    if not db_path.exists():
        logger.error("memory.db 不存在: %s", db_path)
        return 1

    if not args.skip_backup:
        _backup_db(db_path)

    from sgme.data.db import connect_memory
    from sgme.data.search import init_fts

    conn = connect_memory(data_dir)
    try:
        init_fts(conn)
        logger.info("FTS 分词器迁移完成（content_seg 回填 + 索引重建）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
