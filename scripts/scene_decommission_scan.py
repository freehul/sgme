# -*- coding: utf-8 -*-
"""scripts/scene_decommission_scan.py：9.6 场景层下线**只读**扫描（2026-11-04）。

输出：docs/migration/2.0-场景层下线扫描-YYYYMMDD.json（代码消费点 / 库内数据量 /
下线清单）。不删文件、不改库、不打开写连接——410 与四表冻结属生产操作，需窗口确认。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="9.6 场景层下线只读扫描")
    ap.add_argument("--data-dir", default=None, help="数据目录（缺省 <repo>/data）")
    ap.add_argument("--out", default=None, help="报告路径（缺省 docs/migration/...json）")
    ap.add_argument("--root", default=str(ROOT), help="代码根目录（缺省仓库根）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    args = ap.parse_args(argv)

    from sgme.scene_scan import build_report, write_report

    report = build_report(args.root, args.data_dir)
    stamp = datetime.now().strftime("%Y%m%d")
    out = Path(args.out) if args.out else (
        ROOT / "docs" / "migration" / f"2.0-场景层下线扫描-{stamp}.json"
    )
    write_report(report, out)

    code = report["code"]
    summary = {
        "report": str(out),
        "code_files_with_hits": code["files_scanned"],
        "endpoint_hits": code["endpoint_hits"],
        "consumer_files": len(code["consumer_files"]),
        "db_available": report["db"].get("available", False),
        "tables": report["db"].get("tables", {}),
        "checklist": len(report["checklist"]),
    }
    if args.json:
        import json
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"场景层下线扫描（只读）：报告 {out}")
        print(f"- 代码含场景字样的文件：{summary['code_files_with_hits']}；端点命中 {summary['endpoint_hits']}")
        print(f"- 外部消费方（adapters/ui）：{summary['consumer_files']} 个文件")
        print(f"- 库数据：{'可用' if summary['db_available'] else '未找到 memory.db'} {summary['tables']}")
        print(f"- 下线清单 {summary['checklist']} 项（详见报告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
