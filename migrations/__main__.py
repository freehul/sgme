"""migrations/__main__.py：命令行入口（`python -m migrations`）。

⚠️ 生产数据请**先在副本上演练**：
    cp -r data data_migrate_test && python -m migrations --data-dir data_migrate_test
"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数并执行迁移，返回进程退出码。"""
    parser = argparse.ArgumentParser(
        prog="python -m migrations",
        description="SGME v0.7 一次性数据迁移（双库 → 三库存量搬运）",
    )
    parser.add_argument("--data-dir", default=None, help="数据目录（缺省取 config.DATA_DIR）")
    parser.add_argument("--force", action="store_true", help="忽略 applied 标记强制重跑（可重入）")
    parser.add_argument("--dry-run", action="store_true", help="只打印待执行迁移，不落任何改动")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印 INFO 级日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from migrations import run_all

    try:
        report = run_all(data_dir=args.data_dir, force=args.force, dry_run=args.dry_run)
    except Exception as e:
        print(f"迁移失败：{e}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
