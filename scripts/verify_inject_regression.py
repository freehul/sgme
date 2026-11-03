# -*- coding: utf-8 -*-
"""scripts/verify_inject_regression.py：9.4 #9 注入回归对照（只读）。

用法：
  python scripts/verify_inject_regression.py --save-baseline   # 首次建基线
  python scripts/verify_inject_regression.py                   # 与基线比对（差异率 ≤1% 才通过）

基线默认 docs/migration/2.0-inject-regression-baseline.json；
报告默认 docs/migration/2.0-inject-regression-YYYYMMDD.json。
只开 mode=ro 连接，不写库。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _connect_ro(data_dir: Path) -> sqlite3.Connection:
    db_path = data_dir / "memory.db"
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db 不存在: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="9.4 #9 注入回归对照（只读）")
    ap.add_argument("--data-dir", default=None, help="数据目录（缺省 <repo>/data）")
    ap.add_argument("--baseline", default=None, help="基线 JSON（缺省 docs/migration/...）")
    ap.add_argument("--save-baseline", action="store_true", help="把当前结果写成基线")
    ap.add_argument("--threshold", type=float, default=0.01, help="差异率阈值（默认 0.01）")
    ap.add_argument("--out", default=None, help="报告路径（缺省 docs/migration/...）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = ap.parse_args(argv)

    from sgme.regression_inject import (
        compare_results, load_baseline, run_queries, save_baseline, scenes_facts,
    )

    data_dir = Path(args.data_dir) if args.data_dir else ROOT / "data"
    baseline_path = Path(args.baseline) if args.baseline else (
        ROOT / "docs" / "migration" / "2.0-inject-regression-baseline.json"
    )
    stamp = datetime.now().strftime("%Y%m%d")
    out_path = Path(args.out) if args.out else (
        ROOT / "docs" / "migration" / f"2.0-inject-regression-{stamp}.json"
    )

    conn = _connect_ro(data_dir)
    try:
        current = run_queries(conn)
        facts = scenes_facts(conn)
    finally:
        conn.close()

    mode = "save"
    result = None
    if args.save_baseline or not baseline_path.exists():
        save_baseline(current, baseline_path)
    else:
        mode = "compare"
        result = compare_results(current, load_baseline(baseline_path), args.threshold)

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": mode,
        "data_dir": str(data_dir),
        "baseline": str(baseline_path),
        "queries": [{k: q[k] for k in ("name", "count", "hash")} for q in current],
        "comparison": result,
        "scenes_facts": facts,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = True if result is None else bool(result["passed"])
    if args.json:
        print(json.dumps({
            "mode": mode, "report": str(out_path), "baseline": str(baseline_path),
            "passed": passed,
            "comparison": result,
            "inject_has_scene_path": facts["inject_has_scene_path"],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"注入回归（{mode}）：基线 {baseline_path}")
        if result is None:
            print(f"- 已写基线：{len(current)} 条查询；报告 {out_path}")
        else:
            print(f"- 差异 {result['changed']}/{result['total_queries']} "
                  f"（差异率 {result['diff_rate']:.2%}，阈值 {result['threshold']:.0%}）"
                  f" → {'PASS' if result['passed'] else 'FAIL'}")
            for d in result["diffs"]:
                print(f"  · {d['name']}：{d['reason']}")
        print(f"- 注入侧场景引用 {facts['inject_scene_refs']} 处；"
              f"场景表 {facts['scenes_rows']}")
        print(f"- 报告：{out_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
