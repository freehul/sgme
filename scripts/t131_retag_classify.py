# -*- coding: utf-8 -*-
"""T-131 legacy projects/tasks tag governance: pull + dry-run classifier.

Two subcommands:
  pull     Read-only admin-API fetch of projects/tasks-tagged memories,
           dedupe by memory_id (merge dimensions), cache to tmp/t131_raw.json.
  classify Compute the gap set (tagged ONLY projects/tasks, no valid dimension),
           deterministically sample, batch-call the LLM (agnes-2.5-flash via the
           real degradation chain) to propose which valid dimensions to ADD.
           Writes a reviewable proposal (markdown + JSON). NEVER writes production.

Strategy (user-approved 2026-08-31): lightweight re-tag -- ADD valid dimensions,
KEEP projects/tasks labels intact, zero structural change.

Usage:
  python scripts/t131_retag_classify.py pull
  python scripts/t131_retag_classify.py classify [--sample 45] [--seed 7] [--batch 15]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://192.168.10.10:9910"
ENV_PATH = ROOT / "docker.env"
CACHE = ROOT / "tmp" / "t131_raw.json"
PROPOSAL_MD = ROOT / "eval" / "results" / "t131_dryrun_proposal.md"
PROPOSAL_JSON = ROOT / "eval" / "results" / "t131_proposals_sample.json"
DEPRECATED = {"projects", "tasks"}
DIMS_YAML = ROOT / "registry" / "dimensions.yaml"


def load_dotenv(path: Path) -> None:
    """Load .env KEY=VALUE into os.environ (no echo, no overwrite)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_admin_key() -> str:
    text = ENV_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SGME_ADMIN_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("docker.env missing SGME_ADMIN_KEY")


def _api_get(path: str, key: str) -> dict:
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def pull_raw() -> list[dict]:
    """Read-only pull of projects+tasks memories; dedupe by memory_id."""
    key = load_admin_key()
    by_id: dict[str, dict] = {}
    for dim in ("projects", "tasks"):
        page = 1
        seen = 0
        while True:
            data = _api_get(
                f"/v1/admin/memories?dimension_id={dim}&limit=200&page={page}",
                key,
            )
            items = data.get("items") or []
            if not items:
                break
            for it in items:
                mid = it.get("memory_id")
                if not mid:
                    continue
                rec = by_id.setdefault(
                    mid,
                    {
                        "memory_id": mid,
                        "content": it.get("content") or "",
                        "dimensions": set(),
                        "updated_at": it.get("updated_at"),
                    },
                )
                rec["dimensions"].update(it.get("dimensions") or [])
                seen += 1
            total = data.get("total", 0)
            if seen >= total or len(items) < 200:
                break
            page += 1
    out = []
    for rec in by_id.values():
        rec["dimensions"] = sorted(rec["dimensions"])
        out.append(rec)
    CACHE.write_text(
        json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    return out


def load_valid_dims() -> dict[str, dict]:
    """Load canonical valid dimensions (exclude projects/tasks) from registry."""
    import yaml

    data = yaml.safe_load(DIMS_YAML.read_text(encoding="utf-8")) or {}
    dims: dict[str, dict] = {}
    for d in data.get("dimensions", []):
        did = d.get("id")
        if not did or did in DEPRECATED:
            continue
        dims[did] = {
            "display_name": d.get("display_name", ""),
            "description": (d.get("description") or "").split("。")[0],
        }
    return dims


def compute_gap(raw: list[dict]) -> list[dict]:
    """Gap set = memories tagged ONLY projects/tasks (no valid dimension)."""
    gap = []
    for rec in raw:
        dims = set(rec["dimensions"])
        if dims and dims.issubset(DEPRECATED):
            gap.append(rec)
    return gap


def _build_prompt(inp: list[dict], valid_dims: dict[str, dict]) -> str:
    lines = []
    lines.append(
        "You are the SGME memory tag-governance assistant. Each memory below is "
        "currently tagged only with 'projects' or 'tasks' (deprecated structural "
        "dimensions). Decide which VALID dimensions it should ALSO be tagged with, "
        "so it becomes retrievable/injectable."
    )
    lines.append("")
    lines.append("Valid dimensions and their meanings:")
    for did, info in valid_dims.items():
        lines.append(f"- {did} ({info['display_name']}): {info['description']}")
    lines.append("")
    lines.append(
        "Choose ONLY from the dimensions above. If a memory is not relevant to any "
        "valid dimension, return an empty array."
    )
    lines.append("")
    lines.append("Input (JSON array, each item has memory_id and content):")
    lines.append(json.dumps(inp, ensure_ascii=False))
    lines.append("")
    lines.append(
        "Output STRICTLY one JSON object only: keys are memory_id, values are "
        "arrays of dimension ids to ADD:"
    )
    lines.append('Example: {"id1": ["tech_stack"], "id2": []}')
    lines.append("No explanatory text.")
    return "\n".join(lines)


def _parse_json_obj(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s : e + 1])
        except Exception:
            pass
    return {}


def _normalize_dims(proposed: list, valid_dims: dict, name_to_id: dict) -> list:
    out = []
    for d in proposed or []:
        if not isinstance(d, str):
            continue
        d = d.strip()
        if d in valid_dims:
            out.append(d)
        elif d in name_to_id:
            out.append(name_to_id[d])
    # dedupe preserve order
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def classify_records(
    records: list[dict], valid_dims: dict, cfg: dict, name_to_id: dict
) -> tuple[dict, str | None]:
    from sgme.llm import chain

    inp = [
        {"memory_id": r["memory_id"], "content": (r["content"] or "")[:400]}
        for r in records
    ]
    prompt = _build_prompt(inp, valid_dims)
    text, prov, _ = chain.call_with_fallback(cfg, prompt)
    parsed = _parse_json_obj(text)
    result: dict[str, list] = {}
    for r in records:
        mid = r["memory_id"]
        raw_proposed = parsed.get(mid) or parsed.get(str(mid)) or []
        result[mid] = _normalize_dims(raw_proposed, valid_dims, name_to_id)
    return result, prov


def classify_all(
    samples: list[dict], valid_dims: dict, cfg: dict, batch_size: int
) -> tuple[dict, str | None]:
    name_to_id = {info["display_name"]: did for did, info in valid_dims.items()}
    proposals: dict[str, list] = {}
    prov_used = None
    n = len(samples)
    for i in range(0, n, batch_size):
        batch = samples[i : i + batch_size]
        res, prov = classify_records(batch, valid_dims, cfg, name_to_id)
        proposals.update(res)
        prov_used = prov
        print(f"  batch {i // batch_size + 1}: {len(batch)} memories -> {prov}")
    return proposals, prov_used


def render_proposal(
    samples: list[dict],
    proposals: dict,
    gap_total: int,
    raw_total: int,
    valid_dims: dict,
    prov_used: str | None,
) -> str:
    dim_counter: dict[str, int] = {}
    add_count = 0
    none_count = 0
    for mid, dims in proposals.items():
        if dims:
            add_count += 1
            for d in dims:
                dim_counter[d] = dim_counter.get(d, 0) + 1
        else:
            none_count += 1

    L: list[str] = []
    L.append("# T-131 重打标 Dry-Run 提案（抽样）")
    L.append("")
    L.append(f"- 生成时刻: {datetime.now(timezone.utc).isoformat()}")
    L.append(f"- 拉取记忆总数 (projects+tasks 去重): **{raw_total}**")
    L.append(
        f"- 缺口集 (仅 projects/tasks、无有效维度): **{gap_total}** "
        f"({100.0 * gap_total / raw_total:.1f}%)"
    )
    L.append(f"- 本次抽样分类数: **{len(samples)}**")
    L.append(f"- LLM provider: {prov_used}")
    L.append(
        f"- 抽样中拟补打维度数 >0 的记忆: **{add_count}**；无相关维度: **{none_count}**"
    )
    L.append("")
    L.append("## 拟补打维度频次分布（抽样）")
    L.append("")
    L.append("| 维度 | 显示名 | 抽样命中次数 |")
    L.append("|---|---|---|")
    for did in valid_dims:
        c = dim_counter.get(did, 0)
        if c:
            L.append(f"| {did} | {valid_dims[did]['display_name']} | {c} |")
    L.append("")
    L.append("## 抽样明细（memory_id -> 拟补打维度）")
    L.append("")
    L.append("| memory_id | 内容片段 | 拟补打维度 |")
    L.append("|---|---|---|")
    for r in samples:
        mid = r["memory_id"]
        dims = proposals.get(mid, [])
        snippet = (r["content"] or "").replace("\n", " ")[:80]
        L.append(f"| {mid} | {snippet} | {', '.join(dims) if dims else '(none)'} |")
    L.append("")
    L.append("---")
    L.append(
        "确认无误后，将以此提案为基准对【全量缺口集】执行分批补打"
        "（保留 projects/tasks 标签，新增上述有效维度）。"
    )
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull")
    p_cl = sub.add_parser("classify")
    p_cl.add_argument("--sample", type=int, default=45)
    p_cl.add_argument("--seed", type=int, default=7)
    p_cl.add_argument("--batch", type=int, default=15)
    args = ap.parse_args()

    if args.cmd == "pull":
        raw = pull_raw()
        print(f"pulled {len(raw)} records -> {CACHE}")
        return

    # classify
    if CACHE.exists():
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"loaded cache: {len(raw)} records")
    else:
        print("cache missing, pulling...")
        raw = pull_raw()
    gap = compute_gap(raw)
    print(f"gap set: {len(gap)} / {len(raw)}")
    valid_dims = load_valid_dims()
    print(f"valid dims: {len(valid_dims)}")

    rnd = random.Random(args.seed)
    n = min(args.sample, len(gap))
    samples = rnd.sample(gap, n)

    load_dotenv(ROOT / ".env")
    import sgme.config as cfg_mod
    from sgme.llm import chain  # noqa: F401 (ensures import path)

    cfg = cfg_mod.load_llm_config()
    proposals, prov = classify_all(samples, valid_dims, cfg, args.batch)

    md = render_proposal(
        samples, proposals, len(gap), len(raw), valid_dims, prov
    )
    PROPOSAL_MD.write_text(md, encoding="utf-8")
    json.dump(
        {
            "seed": args.seed,
            "sample": len(samples),
            "gap_total": len(gap),
            "raw_total": len(raw),
            "provider": prov,
            "proposals": proposals,
        },
        open(PROPOSAL_JSON, "w", encoding="utf-8"),
        ensure_ascii=False,
        indent=2,
    )
    print(f"proposal -> {PROPOSAL_MD}")
    print(f"json     -> {PROPOSAL_JSON}")


if __name__ == "__main__":
    main()
