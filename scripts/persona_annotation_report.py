# -*- coding: utf-8 -*-
"""scripts/persona_annotation_report.py：D-25 人格/角色标注集覆盖报告（只读）。

用法：
  python scripts/persona_annotation_report.py            # 文本报告
  python scripts/persona_annotation_report.py --json     # 机器读

退出码：结构校验有错误 → 1；否则 0（草稿未满 35 条不算失败，报告里列缺口）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "eval" / "persona" / "cases_v001_draft.yaml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-25 标注集覆盖报告（只读）")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from eval.persona.loader import build_report, load_cases

    data = load_cases(args.file)
    report = build_report(data)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        cov = report["coverage"]
        print(f"D-25 标注集 {report['version']}（user_checked={report['user_checked']}）")
        print(f"- 草稿 {cov['draft_total']}/{cov['target_total']}；缺口 {cov['remaining_total']}")
        for cat, row in cov["by_category"].items():
            print(f"  · {cat}: {row['draft']}/{row['target']}（余 {row['remaining']}）")
        if report["errors"]:
            print("- ❌ 结构错误：")
            for e in report["errors"]:
                print(f"  · {e}")
        else:
            print("- ✅ 结构校验通过")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
