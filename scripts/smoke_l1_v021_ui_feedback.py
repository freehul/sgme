"""scripts/smoke_l1_v021_ui_feedback.py：L1 提示词 v021 真实 LLM 冒烟（W09-W10）。

目的
----
对比 v020（现役）与 v021（UI/操作反馈口径）在 3 类样本上的产出：
  1) 按钮/UI 反馈（一次性反馈 + 可复用规则）
  2) 普通偏好（防回归：偏好/习惯仍能抽出）
  3) 项目设计决策（决策 + 原因 + 适用范围）

**不写生产记忆池**：脚本只做「渲染提示词 → 调 LLM → 解析 JSON → 统计」，
不连接 memory.db、不调 refine/pipeline、不 append 任何会话。

用法
----
    .venv\\Scripts\\python.exe scripts/smoke_l1_v021_ui_feedback.py
    .venv\\Scripts\\python.exe scripts/smoke_l1_v021_ui_feedback.py --versions v020,v021 --repeat 2
    .venv\\Scripts\\python.exe scripts/smoke_l1_v021_ui_feedback.py --dry-run   # 只渲染，不调 LLM

退出码：0 成功 / 2 LLM 不可用（如实报告，不伪造结果）/ 1 其他错误。
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，中文/emoji 输出会炸 → 统一 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import yaml  # noqa: E402

from sgme import config as sgme_config  # noqa: E402
from sgme.engine import l2_extraction  # noqa: E402
from sgme.engine.l2_edges import normalize_unit_text  # noqa: E402  （与 W04 映射器同一归一函数）
from sgme.llm import chain as llm_chain  # noqa: E402
from sgme.llm import provider as llm_provider  # noqa: E402
from sgme.prompts import PromptStore  # noqa: E402

PROMPTS_ROOT = ROOT / "sgme" / "resources" / "prompts"
VERSIONS_DIR = PROMPTS_ROOT / "versions" / "l1_extraction"

# ---------- 3 类样本（通用假值，无真实路径/密钥/人名） ----------

SAMPLES = [
    {
        "id": "ui_feedback",
        "label": "按钮/UI 反馈",
        "conversation": (
            "[msg#1] 2026-11-08T20:12:00 user:\n"
            "  演示页的提交按钮黑字看不清，帮我改成白字。\n"
            "\n"
            "[msg#2] 2026-11-08T20:12:30 assistant:\n"
            "  已把提交按钮改成白字，底色用实色蓝。\n"
            "\n"
            "[msg#3] 2026-11-08T20:13:10 user:\n"
            "  嗯。以后凡是深色底，都别用黑字——对比度不够，统一白字配实色底。\n"
            "  绿底和红底也一样，别只改这一个按钮。\n"
        ),
    },
    {
        "id": "preference",
        "label": "普通偏好",
        "conversation": (
            "[msg#1] 2026-11-08T21:02:00 user:\n"
            "  我周末喜欢逛旧书店，安静、有氛围；人多的商场我一般不去。\n"
            "\n"
            "[msg#2] 2026-11-08T21:02:20 assistant:\n"
            "  旧书店确实更安静。\n"
            "\n"
            "[msg#3] 2026-11-08T21:03:00 user:\n"
            "  还有，我加班到九点以后，回家前会先绕小区走一圈，散散步再上楼。\n"
        ),
    },
    {
        "id": "design_decision",
        "label": "项目设计决策",
        "conversation": (
            "[msg#1] 2026-11-08T21:20:00 user:\n"
            "  AIRDT 的左栏我定成上下两栏：上面放项目操作和列表，下面放导航。\n"
            "\n"
            "[msg#2] 2026-11-08T21:20:30 assistant:\n"
            "  好，我按上下两栏调整。\n"
            "\n"
            "[msg#3] 2026-11-08T21:21:10 user:\n"
            "  原因有三个：一是项目切换频繁，操作区要常驻；二是导航项多，横排会挤；\n"
            "  三是上下分栏在高分屏上留白更少。这个结构别再改回横向。\n"
        ),
    },
]

# ---------- 裸事实/规则启发式判定（人工复核见报告） ----------

UI_RE = re.compile(r"按钮|白字|黑字|底色|颜色|对比度|布局|左栏|界面|导航|报错|点击|上下两栏|UI")
# 过程叙述：主事件/事实里出现「用户要求/反馈/让…」这类动作记录（裸事实的典型指纹）
PROCESS_RE = re.compile(r"用户(反馈|要求|让|提出|希望|询问|叫|先|随后)")
# 一次性操作：单次改动/单次报错/单次布局调整
ONE_OFF_RE = re.compile(r"某次|这次|单次|演示页|一次|改成|改为|调整|修改|修复|重命名|换成")
# 可复用规则/标准/决策措辞
RULE_RE = re.compile(r"统一|一律|禁止|必须|规范|标准|铁律|原则|决策|采用|不再|不得|应|定成|固定为")


def _units_from_text(text: str) -> tuple[list[dict], str | None]:
    """解析 LLM 输出的原始单元列表（保留 main_event/facts/causes）。"""
    try:
        raw = l2_extraction._extract_json_array(text)  # noqa: SLF001 — 冒烟脚本内部使用
    except Exception as e:  # noqa: BLE001
        return [], f"JSON 解析失败: {e}"
    units = []
    for u in raw:
        if not isinstance(u, dict):
            continue
        main = u.get("main_event")
        if isinstance(main, str):
            main = {"content": main}
        if not isinstance(main, dict):
            main = {}
        units.append(
            {
                "main_event": {
                    "content": str(main.get("content") or ""),
                    "occurred_at": main.get("occurred_at"),
                    "feeling": main.get("feeling"),
                },
                "facts": [f for f in (u.get("facts") or []) if isinstance(f, dict)],
                "causes": [c for c in (u.get("causes") or []) if isinstance(c, dict)],
                "trait": u.get("trait"),
                "dimensions": u.get("dimensions"),
                "route": u.get("route"),
            }
        )
    return units, None


def classify(unit: dict) -> dict:
    """单单元启发式分类（报告里同时给人工复核结论）。

    裸事实口径（严格）：UI/操作类单元 + 无 causes + 主事件/事实是过程叙述或一次性操作。
    规则口径：有 causes（原因 → 结论）且含规则/标准/决策措辞。
    """
    main = str((unit.get("main_event") or {}).get("content") or "")
    facts = unit.get("facts") or []
    causes = unit.get("causes") or []
    fact_text = " ".join(str(f.get("content") or "") for f in facts)
    text = main + " " + fact_text
    is_ui = bool(UI_RE.search(text))
    has_causes = bool(causes)
    process_narrative = bool(PROCESS_RE.search(text))
    one_off = bool(ONE_OFF_RE.search(text))
    rule_marker = bool(RULE_RE.search(text))
    # 裸事实：UI/操作类 + 没有因果 + 只是过程叙述或一次性操作
    naked_ui = is_ui and not has_causes and (process_narrative or one_off)
    # 规则/决策：有因果 + 规则/标准/决策措辞
    rule_with_cause = bool(has_causes and (rule_marker or is_ui))
    return {
        "is_ui": is_ui,
        "process_narrative": process_narrative,
        "one_off": one_off,
        "rule_marker": rule_marker,
        "has_causes": has_causes,
        "naked_ui_fact": naked_ui,
        "rule_with_cause": rule_with_cause,
        "fact_types": [str(f.get("type") or "") for f in facts],
        "fact_count": len(facts),
    }


def cause_stats(unit: dict) -> dict:
    """causes 端点 exact-match 统计（对同一单元 main_event/facts 的原文）。

    口径与 W04 映射器对齐：
    - exact：端点 strip 后与某条原文 strip 后相等（提示词允许前后空白差异）；
    - normalized：用 l2_edges.normalize_unit_text 归一后相等（映射器实际比较口径）。
    """
    main = str((unit.get("main_event") or {}).get("content") or "")
    facts = [f for f in (unit.get("facts") or []) if isinstance(f, dict)]
    contents = [c.strip() for c in [main, *[str(f.get("content") or "") for f in facts]] if c.strip()]
    exact_set = set(contents)
    norm_set = {normalize_unit_text(c) for c in contents}
    causes = [c for c in (unit.get("causes") or []) if isinstance(c, dict)]
    endpoints = [
        v for c in causes for v in (c.get("from"), c.get("to"))
        if isinstance(v, str) and v.strip()
    ]
    return {
        "causes": len(causes),
        "endpoints": len(endpoints),
        "exact": sum(1 for v in endpoints if v.strip() in exact_set),
        "normalized": sum(1 for v in endpoints if normalize_unit_text(v) in norm_set),
    }


def _read_version(version: str) -> str:
    path = VERSIONS_DIR / f"{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"版本文件不存在: {path}")
    return path.read_text(encoding="utf-8")


def _dimensions() -> list[dict]:
    raw = yaml.safe_load(
        (ROOT / "sgme" / "resources" / "registry" / "dimensions.yaml").read_text(encoding="utf-8")
    )
    return raw["dimensions"]


def _render(template: str, conversation: str, dims: list[dict]) -> str:
    return l2_extraction._render_extraction_text(template, conversation, dims)  # noqa: SLF001


def _summarize(runs: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in runs:
        raw_key = r["version"]
        scope = r.get("provider_scope")
        key = f"{scope}::{raw_key}" if scope and scope != "auto" else raw_key
        if r.get("error"):
            st = out.setdefault(key, {"runs": 0, "error_runs": 0})
            st["error_runs"] += 1
            continue
        s = out.setdefault(
            key,
            {
                "runs": 0,
                "error_runs": 0,
                "samples": {},
                "units": 0,
                "ui_units": 0,
                "naked_ui_facts": 0,
                "ui_rule_with_cause": 0,
                "units_with_causes": 0,
                "process_narrative_units": 0,
                "one_off_mention_units": 0,
                "facts_total": 0,
                "causes": 0,
                "cause_endpoints": 0,
                "cause_endpoints_exact": 0,
                "cause_endpoints_normalized": 0,
            },
        )
        s["runs"] += 1
        naked = sum(1 for u in r["classified"] if u["naked_ui_fact"])
        rule = sum(1 for u in r["classified"] if u["rule_with_cause"])
        cstats = r.get("cause_stats") or []
        sample_exact = sum(c["exact"] for c in cstats)
        sample_endpoints = sum(c["endpoints"] for c in cstats)
        s["samples"].setdefault(r["sample_id"], []).append(
            {
                "repeat": r["repeat"],
                "units": len(r["units"]),
                "naked_ui_facts": naked,
                "rule_with_cause": rule,
                "cause_endpoints": sample_endpoints,
                "cause_endpoints_exact": sample_exact,
                "provider": r.get("provider"),
                "elapsed_s": r.get("elapsed_s"),
            }
        )
        s["units"] += len(r["units"])
        for c in cstats:
            s["causes"] += c["causes"]
            s["cause_endpoints"] += c["endpoints"]
            s["cause_endpoints_exact"] += c["exact"]
            s["cause_endpoints_normalized"] += c["normalized"]
        for c in r["classified"]:
            s["facts_total"] += c["fact_count"]
            if c["is_ui"]:
                s["ui_units"] += 1
            if c["naked_ui_fact"]:
                s["naked_ui_facts"] += 1
            if c["rule_with_cause"]:
                s["ui_rule_with_cause"] += 1
            if c["has_causes"]:
                s["units_with_causes"] += 1
            if c["process_narrative"]:
                s["process_narrative_units"] += 1
            if c["one_off"]:
                s["one_off_mention_units"] += 1
    for s in out.values():
        s.setdefault("units", 0)
        s.setdefault("ui_units", 0)
        s.setdefault("naked_ui_facts", 0)
        s.setdefault("ui_rule_with_cause", 0)
        s.setdefault("units_with_causes", 0)
        s.setdefault("process_narrative_units", 0)
        s.setdefault("one_off_mention_units", 0)
        s.setdefault("facts_total", 0)
        s.setdefault("causes", 0)
        s.setdefault("cause_endpoints", 0)
        s.setdefault("cause_endpoints_exact", 0)
        s.setdefault("cause_endpoints_normalized", 0)
        s.setdefault("samples", {})
        s["naked_ui_fact_rate"] = (
            round(s["naked_ui_facts"] / s["ui_units"], 3) if s["ui_units"] else None
        )
        s["ui_rule_with_cause_rate"] = (
            round(s["ui_rule_with_cause"] / s["ui_units"], 3) if s["ui_units"] else None
        )
        s["cause_coverage"] = (
            round(s["units_with_causes"] / s["units"], 3) if s["units"] else None
        )
        s["cause_endpoint_exact_rate"] = (
            round(s["cause_endpoints_exact"] / s["cause_endpoints"], 3)
            if s["cause_endpoints"] else None
        )
        s["cause_endpoint_normalized_rate"] = (
            round(s["cause_endpoints_normalized"] / s["cause_endpoints"], 3)
            if s["cause_endpoints"] else None
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L1 提示词真实 LLM 冒烟（3 类样本 + causes 端点 exact-match）")
    parser.add_argument("--versions", default="v021,v022", help="对比版本，逗号分隔（默认 v021,v022）")
    parser.add_argument("--samples", default="all", help="样本 id 逗号分隔，all=全部 3 类")
    parser.add_argument("--repeat", type=int, default=1, help="每样本每版本重复次数（默认 1）")
    parser.add_argument("--out", default="", help="结果 JSON 输出路径（默认 tmp/smoke_l1_v021_<ts>.json）")
    parser.add_argument("--dry-run", action="store_true", help="只渲染提示词，不调 LLM")
    parser.add_argument(
        "--evaluate-json", default="",
        help="复用已有结果 JSON 的 raw_text 重新分类统计（不调 LLM，零成本复算）",
    )
    parser.add_argument(
        "--providers", default="",
        help="强制 provider（逗号分隔，如 zhipu,siliconflow）；空=按 refinement 链自动降级",
    )
    args = parser.parse_args(argv)

    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    sample_ids = {s.strip() for s in args.samples.split(",") if s.strip()}
    samples = [s for s in SAMPLES if args.samples == "all" or s["id"] in sample_ids]
    if not samples:
        print(f"无匹配样本: {args.samples}", file=sys.stderr)
        return 1
    dims = _dimensions()
    templates = {v: _read_version(v) for v in versions}

    if args.dry_run:
        for v in versions:
            pv = PromptStore().get("l1_extraction", None)
            print(f"[dry-run] {v}: {len(templates[v])} chars；PromptStore active={pv.version}")
            for s in samples:
                prompt = _render(templates[v], s["conversation"], dims)
                print(f"  - {s['id']}: rendered {len(prompt)} chars")
        return 0

    if args.evaluate_json:
        src = json.loads(Path(args.evaluate_json).read_text(encoding="utf-8"))
        runs = []
        for r in src.get("runs") or []:
            if r.get("error"):
                runs.append(r)
                continue
            units, parse_err = _units_from_text(r.get("raw_text") or "")
            r = dict(r)
            r["units"] = units
            r["classified"] = [classify(u) for u in units]
            r["cause_stats"] = [cause_stats(u) for u in units]
            r["parse_error"] = parse_err
            runs.append(r)
        summary = _summarize(runs)
        src["runs"] = runs
        src["summary"] = summary
        src["evaluated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out_path = Path(args.out) if args.out else Path(args.evaluate_json).with_suffix(".recalc.json")
        out_path.write_text(json.dumps(src, ensure_ascii=False, indent=2), encoding="utf-8")
        for v in summary:
            s = summary.get(v) or {}
            print(
                f"  {v}: units={s.get('units')} UI={s.get('ui_units')} "
                f"裸事实={s.get('naked_ui_facts')} 裸事实率={s.get('naked_ui_fact_rate')} "
                f"UI规则带因果={s.get('ui_rule_with_cause')} "
                f"规则带因果率={s.get('ui_rule_with_cause_rate')} 因果覆盖={s.get('cause_coverage')} "
                f"causes端点 exact={s.get('cause_endpoints_exact')}/{s.get('cause_endpoints')} "
                f"exact率={s.get('cause_endpoint_exact_rate')}"
            )
        print(f"  result_json: {out_path}")
        return 0

    sgme_config.load_env_file()
    llm_cfg = llm_chain.load_config()
    chain = (llm_cfg.get("chains") or {}).get("refinement") or []

    # W09c：--providers 强制指定 provider（单节点，不降级），用于跨模型对照；
    # 缺省 = 按 refinement 链自动降级（既有行为）。
    forced = [p.strip() for p in args.providers.split(",") if p.strip()]
    provider_cfgs: dict[str, dict] = {}
    if forced:
        chain_map = {str(n.get("provider")): n for n in chain}
        for p in forced:
            if p not in chain_map:
                print(f"provider {p} 不在 refinement 链内: {list(chain_map)}", file=sys.stderr)
                return 1
            cfg_p = copy.deepcopy(llm_cfg)
            cfg_p["chains"]["refinement"] = [copy.deepcopy(chain_map[p])]
            provider_cfgs[p] = cfg_p
    else:
        provider_cfgs["auto"] = llm_cfg

    print("=" * 72)
    print("L1 提示词真实 LLM 冒烟（v022 causes 端点复测；不写生产记忆池）")
    print(f"  chain: {' → '.join(str(n.get('provider')) + '/' + str(n.get('model')) for n in chain)}")
    print(f"  providers: {list(provider_cfgs)}（forced={'是' if forced else '否'}）")
    print(f"  versions: {versions}  samples: {[s['id'] for s in samples]}  repeat: {args.repeat}")
    print("=" * 72)

    runs: list[dict] = []
    started = datetime.now(timezone.utc)
    for scope, scope_cfg in provider_cfgs.items():
        for version in versions:
            for sample in samples:
                for rep in range(1, args.repeat + 1):
                    prompt = _render(templates[version], sample["conversation"], dims)
                    t0 = time.monotonic()
                    try:
                        text, provider_name, usage = llm_chain.call_with_fallback(
                            scope_cfg, prompt, chain_name="refinement"
                        )
                    except llm_provider.LLMError as e:
                        # 402/401/400 等非 fallback 错误会原样抛出（LLMError 子类）；
                        # 这里统一记录类型+原文，跨模型对照时如实保留 provider 不可用原因。
                        print(
                            f"[FAIL] [{scope}] {version}/{sample['id']}#{rep} "
                            f"{type(e).__name__}: {e}",
                            file=sys.stderr,
                        )
                        runs.append({
                            "version": version, "sample_id": sample["id"], "repeat": rep,
                            "provider": None, "provider_scope": scope,
                            "error": f"{type(e).__name__}: {e}",
                        })
                        continue
                    elapsed = round(time.monotonic() - t0, 2)
                    units, parse_err = _units_from_text(text)
                    classified = [classify(u) for u in units]
                    cstats = [cause_stats(u) for u in units]
                    rec = {
                        "version": version,
                        "sample_id": sample["id"],
                        "sample_label": sample["label"],
                        "repeat": rep,
                        "provider": provider_name,
                        "provider_scope": scope,
                        "usage": usage,
                        "elapsed_s": elapsed,
                        "raw_text": text,
                        "units": units,
                        "classified": classified,
                        "cause_stats": cstats,
                        "parse_error": parse_err,
                    }
                    runs.append(rec)
                    naked = sum(1 for c in classified if c["naked_ui_fact"])
                    rules = sum(1 for c in classified if c["rule_with_cause"])
                    causes = sum(1 for c in classified if c["has_causes"])
                    ep = sum(c["endpoints"] for c in cstats)
                    exact = sum(c["exact"] for c in cstats)
                    print(
                        f"[OK] [{scope}] {version}/{sample['id']}#{rep} provider={provider_name} "
                        f"units={len(units)} 裸事实={naked} 规则(带因果)={rules} 有因果={causes} "
                        f"causes端点 exact={exact}/{ep} {elapsed}s"
                        + (f" parse_error={parse_err}" if parse_err else "")
                    )

    summary = _summarize(runs)
    result = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chain": chain,
        "providers": list(provider_cfgs),
        "forced_providers": forced,
        "versions": versions,
        "repeat": args.repeat,
        "samples": [{"id": s["id"], "label": s["label"], "conversation": s["conversation"]} for s in samples],
        "summary": summary,
        "runs": runs,
    }
    out_path = Path(args.out) if args.out else (
        ROOT / "tmp" / f"smoke_l1_v021_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("-" * 72)
    for v in summary:
        s = summary.get(v) or {}
        print(
            f"  {v}: runs={s.get('runs')} error={s.get('error_runs')} units={s.get('units')} "
            f"UI={s.get('ui_units')} 裸事实={s.get('naked_ui_facts')} "
            f"裸事实率={s.get('naked_ui_fact_rate')} UI规则带因果={s.get('ui_rule_with_cause')} "
            f"规则带因果率={s.get('ui_rule_with_cause_rate')} 因果覆盖={s.get('cause_coverage')} "
            f"causes端点 exact={s.get('cause_endpoints_exact')}/{s.get('cause_endpoints')} "
            f"exact率={s.get('cause_endpoint_exact_rate')} "
            f"normalized率={s.get('cause_endpoint_normalized_rate')}"
        )
    print(f"  result_json: {out_path}")
    print("=" * 72)
    if any(r.get("error") for r in runs):
        print("存在 LLM 不可用/失败样本：结果不完整，请如实报告，勿伪造。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
