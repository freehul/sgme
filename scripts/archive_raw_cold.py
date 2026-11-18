# -*- coding: utf-8 -*-
"""scripts/archive_raw_cold.py：D-17 冷归档运维入口（T-422，2026-11-18）。

给 ``sgme.backup.manager.archive_raw_cold`` 的 require_refined 门控一个可执行
调用方：>N 天未修改 **且已完成 L2 提炼**（raw_files.status='refined'）的会话
压缩为冷归档；未提炼 / 状态查不到一律保守跳过（宁可不归档）。归档 = 压缩搬家，
原件保留（D-17 来时路原则）。

用法
----
    python scripts/archive_raw_cold.py --days 90            # dry-run：只报将归档清单
    python scripts/archive_raw_cold.py --days 90 --apply    # 实际压缩
    python scripts/archive_raw_cold.py --days 90 --apply --skip-gate   # 显式豁免门控（手工兜底）

数据目录 / raw 目录默认取 sgme.config（生产由 SGME_HOME 决定），也可 --data-dir
/--raw-dir 显式指定——脚本内不写死任何本机路径。归档目录默认 <backup_dir>/raw_archive。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme import config as sgme_config  # noqa: E402
from sgme.backup import manager  # noqa: E402
from sgme.data import db as db_mod  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="D-17 冷归档（已提炼会话才归档，dry-run 默认）")
    ap.add_argument("--days", type=int, default=90, help="N 天未修改才归档（默认 90）")
    ap.add_argument("--apply", action="store_true", help="实际执行压缩（缺省 dry-run 只报告）")
    ap.add_argument("--skip-gate", action="store_true",
                    help="显式豁免 D-17 门控（require_refined=False，手工兜底用）")
    ap.add_argument("--data-dir", default=None, help="数据库目录（缺省走 sgme.config）")
    ap.add_argument("--raw-dir", default=None, help="raw 目录（缺省走 sgme.config）")
    ap.add_argument("--archive-dir", default=None,
                    help="归档输出目录（缺省 <backup_dir>/raw_archive）")
    a = ap.parse_args()

    d = Path(a.data_dir) if a.data_dir else sgme_config.DATA_DIR
    raw_dir = Path(a.raw_dir) if a.raw_dir else sgme_config.RAW_DIR
    archive_dir = Path(a.archive_dir) if a.archive_dir else (
        sgme_config.resolve_backup_dir(sgme_config.load_config()) / "raw_archive")

    session_conn = db_mod.connect_session(d)
    try:
        out = manager.archive_raw_cold(
            raw_dir, archive_dir, days=a.days,
            require_refined=not a.skip_gate,
            session_conn=session_conn,
        )
    finally:
        db_mod.close(session_conn)

    mode = "APPLY" if a.apply else "DRY-RUN"
    gate = "off（显式豁免）" if a.skip_gate else "on（只归档已提炼）"
    print(f"[{mode}] days={a.days} 门控={gate}")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not a.apply:
        print("dry-run 未写任何文件；确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
