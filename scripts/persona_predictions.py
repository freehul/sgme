# -*- coding: utf-8 -*-
"""scripts/persona_predictions.py：D-25 标注集预测生成（调用真实提炼链，需额度）。

把标注集的 conversation 逐条送进 SGME 的 L1 提取链（l1.extract_l1，含提示词渲染、
分块、重试、JSON 解析），产出七指标跑分所需的 predictions JSON。

用法：
  python scripts/persona_predictions.py --dry-run                 # 只列将跑的用例
  python scripts/persona_predictions.py --limit 1 --out tmp/p1.json  # 小样本验证链路
  python scripts/persona_predictions.py --out eval/persona/preds.json  # 全量 35 条

边界：
- 会真实调用 LLM（按 config/llm.yaml 降级链），消耗额度；--dry-run 不调用；
- 只读标注集、只写 --out 指定的 JSON；不写 SGME 库、不落 L0；
- causal 目前不生成（L1 不产出因果边），七指标的因果误连率按「预测无边 → 0」计。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLD = ROOT / "eval" / "persona" / "cases_v001_draft.yaml"


def _predict_one(case: dict, dimensions: list[dict], llm_cfg: dict) -> dict:
    from sgme.engine import l1

    memories, provider, meta = l1.extract_l1(
        str(case["conversation"]), dimensions, llm_cfg,
    )
    out = []
    for m in memories:
        dims = [str(d) for d in (m.get("dimensions") or [])]
        out.append({
            "content": m.get("content"),
            "dimensions": dims,
            "main_dimension": dims[0] if dims else None,
            "memory_type": m.get("memory_type"),
            "certainty": m.get("certainty"),
            "subject": m.get("subject"),
            "route": m.get("route") or "memory",
            "occurred_at": m.get("occurred_at"),
        })
    return {"memories": out, "causal": [], "traits": [], "_provider": provider,
            "_prompt": meta}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-25 预测生成（真实提炼链）")
    ap.add_argument("--gold", default=str(DEFAULT_GOLD))
    ap.add_argument("--out", default="tmp/persona_predictions.json")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    ap.add_argument("--only", default="", help="逗号分隔 case id（如 pa-001,pa-004）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="每条之间等待秒数（防限流；建议 5-10）")
    args = ap.parse_args(argv)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from eval.persona.loader import load_cases

    data = load_cases(args.gold)
    cases = data["cases"]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit and args.limit > 0:
        cases = cases[: args.limit]

    if args.dry_run:
        print(f"将跑 {len(cases)} 条：{', '.join(c['id'] for c in cases)}")
        return 0

    from sgme.config import load_config

    cfg = load_config()
    dimensions = cfg.get("dimensions")
    if not dimensions:
        from sgme.config import load_dimensions

        dimensions = load_dimensions()
    llm_cfg = cfg.get("llm") or {}

    preds: dict[str, dict] = {}
    providers: set[str] = set()
    errors: list[dict] = []
    started = time.time()
    for idx, case in enumerate(cases, 1):
        if idx > 1 and args.sleep > 0:
            time.sleep(args.sleep)
        cid = case["id"]
        try:
            pred = _predict_one(case, dimensions, llm_cfg)
            providers.add(str(pred.get("_provider")))
            pred.pop("_provider", None)
            pred.pop("_prompt", None)
            preds[cid] = pred
            print(f"[{idx}/{len(cases)}] {cid}: {len(pred['memories'])} 条记忆")
        except Exception as exc:  # noqa: BLE001
            errors.append({"case_id": cid, "error": str(exc)[:300]})
            preds[cid] = {"memories": [], "causal": [], "traits": []}
            print(f"[{idx}/{len(cases)}] {cid}: ERROR {exc}")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "annotations_version": (data.get("meta") or {}).get("version"),
        "providers": sorted(p for p in providers if p),
        "elapsed_s": round(time.time() - started, 1),
        "errors": errors,
        "predictions": preds,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}（{len(preds)} 条预测；错误 {len(errors)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
