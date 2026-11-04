# -*- coding: utf-8 -*-
"""scripts/persona_metrics_report.py：D-25 七指标跑分（骨架，不调 LLM）。

用法：
  python scripts/persona_metrics_report.py --predictions preds.json
  python scripts/persona_metrics_report.py --predictions preds.json --json

预测格式：{"case_id": {"memories": [...], "causal": [...], "traits": [...]}}
退出码：任一指标未达标（passed=false）→ 1；null 指标不算失败。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLD = ROOT / "eval" / "persona" / "cases_v001_draft.yaml"


def _fmt(v):
    if v is None:
        return "—"
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-25 七指标跑分（只读）")
    ap.add_argument("--gold", default=str(DEFAULT_GOLD))
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from eval.persona.loader import load_cases
    from eval.persona.metrics import evaluate

    gold = load_cases(args.gold)
    raw = json.loads(Path(args.predictions).read_text(encoding="utf-8"))
    preds = raw.get("predictions") if isinstance(raw, dict) and "predictions" in raw else raw
    result = evaluate(gold["cases"], preds)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"D-25 七指标跑分：{result['cases']} 例；匹配 {result['matched']} 对")
        for name, chk in result["checks"].items():
            mark = "✅" if chk["passed"] else ("—" if chk["passed"] is None else "❌")
            print(f"  {mark} {name}: {_fmt(chk['value'])}（目标 {chk['target']}）")
        for note in result["notes"]:
            print(f"  · {note}")
    failed = [n for n, c in result["checks"].items() if c["passed"] is False]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
