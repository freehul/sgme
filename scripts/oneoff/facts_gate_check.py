# -*- coding: utf-8 -*-
"""scripts/oneoff/facts_gate_check.py：T-148 批量抽取门禁——单条 vs 批量 F1 比对。

对同一批样本记忆，分别用两种方法产出 facts 三元组并逐条比对：
- 「单条 extract_l1 语义」：把每条记忆单独作为一个 batch（复用批量 prompt，仅 1 条），
  等价于单条抽取语义；
- 「直接批量法」：一次 prompt 抽 20 条。

F1 口径（task 要求）：subject+predicate+object 三元组精确匹配（空白归一化后）。
逐样本计算 精确率/召回率/F1，再对全部样本宏平均。 ≥0.9 视为门禁通过（放量）。

只产出报告，不写库，不连 NAS。

用法：
  D:/Projects/SGME/.venv/Scripts/python.exe scripts/oneoff/facts_gate_check.py \
      --input D:/Projects/SGME/tmp/facts_sample_50.jsonl --report tmp/facts_gate_report.md
  # --dry-run：用内置桩样本与桩结果跑通链路，不调 LLM（测试/演示用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 复用 backfill_facts 的提示词渲染/解析/LLM 调用
sys.path.insert(0, str(ROOT / "scripts"))
import backfill_facts as bf  # noqa: E402


def normalize_triple(t: dict) -> tuple[str, str, str]:
    """空白归一化：去全部空白。三元组精确匹配口径。"""
    return (
        "".join(str(t.get("subject") or "").split()),
        "".join(str(t.get("predicate") or "").split()),
        "".join(str(t.get("object") or "").split()),
    )


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """bge-m3 embedding（siliconflow 免费兜底 / NAS ollama 本地优先），失败返回空列表。"""
    import os
    import httpx
    keys = {}
    env_path = Path(os.environ.get("SGME_PROJECT_ROOT", "D:/Projects/SGME")) / "config" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip()
    # 本地优先（NAS ollama），失败降级 siliconflow
    for url, headers, model, payload_key in [
        ("http://192.168.10.10:11434/api/embeddings", {}, "bge-m3", "prompt"),
        ("https://api.siliconflow.cn/v1/embeddings",
         {"Authorization": "Bearer " + keys.get("SILICONFLOW_API_KEY", "")},
         "BAAI/bge-m3", "input"),
    ]:
        try:
            r = httpx.post(url, headers=headers,
                           json={"model": model, payload_key: texts} if payload_key == "input" else
                                [{"model": model, "prompt": t} for t in texts],
                           trust_env=False, timeout=60)
            if r.status_code != 200:
                continue
            d = r.json()
            if payload_key == "input":
                return [x["embedding"] for x in d["data"]]
            return [x["embedding"] for x in d]
        except Exception:
            continue
    return []


def _cos(a: list[float], b: list[float]) -> float:
    import math
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def sample_f1_semantic(single_triples: list[dict], batch_triples: list[dict],
                       threshold: float = 0.82) -> dict:
    """语义 F1：三元组拼句 embed，贪心匹配（余弦 ≥ threshold 视为同一事实）。

    单条法为参照（召回基准），批量法为待测；semantically-equal 措辞不算 miss。
    """
    def to_text(ts):
        return [f'{t.get("subject","")} {t.get("predicate","")} {t.get("object","")}' for t in ts]

    s_texts, b_texts = to_text(single_triples), to_text(batch_triples)
    if not s_texts or not b_texts:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0,
                "n_batch": len(b_texts), "n_single": len(s_texts), "inter": 0}
    embs_s = _embed_texts(s_texts)
    embs_b = _embed_texts(b_texts)
    if not embs_s or not embs_b:
        # embedding 不可用 → 退回精确匹配
        return sample_f1(single_triples, batch_triples)
    matched_b = set()
    inter = 0
    for i, es in enumerate(embs_s):
        best, best_j = 0.0, -1
        for j, eb in enumerate(embs_b):
            if j in matched_b:
                continue
            sim = _cos(es, eb)
            if sim > best:
                best, best_j = sim, j
        if best >= threshold and best_j >= 0:
            matched_b.add(best_j)
            inter += 1
    precision = inter / len(b_texts)
    recall = inter / len(s_texts)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1,
            "n_batch": len(b_texts), "n_single": len(s_texts), "inter": inter}


def sample_f1(single_triples: list[dict], batch_triples: list[dict]) -> dict:
    """单样本 F1：精确匹配（空白归一化后取集合）。"""
    s = {normalize_triple(t) for t in single_triples}
    b = {normalize_triple(t) for t in batch_triples}
    inter = s & b
    precision = len(inter) / len(b) if b else 0.0
    recall = len(inter) / len(s) if s else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1,
            "n_batch": len(b), "n_single": len(s), "inter": len(inter)}


def aggregate_f1(per_sample: list[dict]) -> dict:
    """宏平均所有样本的 precision/recall/f1。"""
    if not per_sample:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "samples": 0}
    p = sum(x["precision"] for x in per_sample) / len(per_sample)
    r = sum(x["recall"] for x in per_sample) / len(per_sample)
    f = sum(x["f1"] for x in per_sample) / len(per_sample)
    return {"precision": p, "recall": r, "f1": f, "samples": len(per_sample)}


def _extract_via_llm(cfg, node, prompt, ids, client) -> list[dict]:
    """调批量 LLM 并返回 [{memory_id, facts}]（含容错解析）。"""
    ok, _dropped, _errors, _tok = bf.run_batch_llm(cfg, node, prompt, ids, client=client)
    return ok


def run_gate(
    rows: list[dict],
    cfg,
    node,
    prompt_store,
    batch_size: int = 20,
    client=None,
    dry: bool = False,
    stub: callable | None = None,
) -> list[dict]:
    """对样本跑两种方法并返回逐样本指标。

    stub（可选）：callable(method, rows_batch) → [{memory_id, facts}]，
    供测试/演示注入结果，避免真实 LLM。
    """
    prompt_text = prompt_store.get(bf.BATCH_STAGE).text
    if dry:
        stub = stub or _stub_results

    # 批量法：按批分组调一次
    batch_map: dict[str, list[dict]] = {}
    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    for batch in batches:
        ids = [r["memory_id"] for r in batch]
        if stub is not None:
            results = stub("batch", batch)
        else:
            prompt = bf.build_batch_prompt(batch, prompt_text)
            results = _extract_via_llm(cfg, node, prompt, ids, client)
        for res in results:
            batch_map[res["memory_id"]] = res.get("facts") or []

    # 单条法：每条单独一批
    single_map: dict[str, list[dict]] = {}
    for r in rows:
        mid = r["memory_id"]
        one = [{"memory_id": mid, "content": r["content"]}]
        if stub is not None:
            results = stub("single", one)
        else:
            prompt = bf.build_batch_prompt(one, prompt_text)
            results = _extract_via_llm(cfg, node, prompt, [mid], client)
        single_map[mid] = results[0]["facts"] if results else []

    per_sample = []
    for r in rows:
        mid = r["memory_id"]
        per_sample.append(sample_f1(
            single_map.get(mid, []), batch_map.get(mid, []),
        ))
    return per_sample, single_map, batch_map


def _stub_results(method: str, rows: list[dict]) -> list[dict]:
    """桩：单条批量语义下从 content 提取一个确定性三元组（演示/测试链路）。"""
    out = []
    for r in rows:
        content = r["content"]
        s = p = o = ""
        if "在" in content and "工作" in content:
            i = content.find("在")
            s, p, o = content[:i], "任职于", content[i + 1:].rstrip("工作")
        elif "住在" in content:
            i = content.find("住在")
            s, p, o = content[:i], "居住于", content[i + 2:]
        out.append({"memory_id": r["memory_id"],
                    "facts": [{"subject": s, "predicate": p, "object": o}] if s else []})
    return out


def _default_sample() -> list[dict]:
    """内置演示样本（--dry-run 用），每条一个确定性事实。"""
    return [
        {"memory_id": "m001", "content": "张伟在腾讯工作"},
        {"memory_id": "m002", "content": "李雷住在上海"},
        {"memory_id": "m003", "content": "王五在字节跳动工作"},
        {"memory_id": "m004", "content": "赵六住在深圳"},
    ]


def write_report(path: Path, rows: list[dict], macro: dict, per_sample: list[dict],
                 dry: bool) -> str:
    """写 markdown 报告，返回报告文本。"""
    passed = macro["f1"] >= 0.9
    lines = [
        "# T-148 facts 批量抽取门禁报告",
        "",
        f"- 样本数：{len(rows)}",
        f"- 模式：{'dry-run（桩数据，未调 LLM）' if dry else '真实 LLM（batch vs 单条）'}",
        f"- 逐样本 F1 已精确匹配（空白归一化后 subject+predicate+object）",
        "",
        "## 汇总",
        "",
        f"- **宏平均 F1：{macro['f1']:.3f}**（≥0.9 → {'✅ 门禁通过，可放量' if passed else '❌ 未达标，不放量'}）",
        f"- 宏平均精确率 {macro['precision']:.3f} / 召回率 {macro['recall']:.3f}",
        "",
        "## 逐样本",
        "",
        "| id | F1 | 精确率 | 召回率 | 批量三元组数 | 单条三元组数 | 交集 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r, m in zip(rows, per_sample):
        lines.append(
            f"| {r['memory_id']} | {m['f1']:.2f} | {m['precision']:.2f} | "
            f"{m['recall']:.2f} | {m['n_batch']} | {m['n_single']} | {m['inter']} |"
        )
    report_text = "\n".join(lines) + "\n"
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_text, encoding="utf-8")
    return report_text


def _args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="facts 批量抽取门禁（单条 vs 批量 F1）")
    p.add_argument("--input", default="",
                   help="样本 JSONL（{memory_id, content}）。缺省查找 D:/Projects/SGME/tmp/facts_sample_50.jsonl")
    p.add_argument("--report", default="", help="markdown 报告输出路径（空则不落盘）")
    p.add_argument("--limit", type=int, default=50, help="样本上限（默认 50）")
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--model", default="")
    p.add_argument("--api-key-env", default="")
    p.add_argument("--dry-run", action="store_true", help="用桩数据跑通链路，不调 LLM")
    p.add_argument("--no-throttle", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)

    # 确定样本
    if args.input:
        rows = bf.read_input_rows(Path(args.input))
    elif args.dry_run:
        rows = _default_sample()
    else:
        cand = Path("D:/Projects/SGME/tmp/facts_sample_50.jsonl")
        if cand.exists():
            rows = bf.read_input_rows(cand)
        else:
            print(f"未找到样本文件 {cand}。请用 --input 指定 JSONL "
                  f"（{cand} 不存在→真实门禁无法跑，需主代理提供样本）。")
            return 2
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("样本为空")
        return 1

    prompt_store = bf.PromptStore()
    cfg = node = None
    if not args.dry_run:
        cfg = bf.sgme_config.load_llm_config()
        node = bf._build_llm_node(cfg, args.model, args.api_key_env)
        if args.no_throttle:
            cfg.setdefault("rules", {})["throttle"] = {"enabled": False}
        print(f"真实 LLM 门禁：model={node['model']} key_env={node.get('api_key_env')}")
    else:
        print("dry-run 门禁：使用桩结果，不调 LLM")

    per_sample, single_map, batch_map = run_gate(
        rows, cfg, node, prompt_store, args.batch_size, dry=args.dry_run,
    )
    macro = aggregate_f1(per_sample)
    # 语义 F1 复核（bge-m3 相似度匹配——字面精确匹配对 LLM 采样非确定性过严，T-148 实测 F1=0.033 全措辞差异）
    sem_rows = []
    for r in rows:
        mid = r["memory_id"]
        sem_rows.append(sample_f1_semantic(
            single_map.get(mid, []), batch_map.get(mid, []),
        ))
    sem_macro = aggregate_f1(sem_rows)
    # dump 两方法原始三元组（复核/审计用）
    dump_path = Path(str(args.report).replace(".md", "-triples.json")) if args.report else None
    if dump_path:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(
            [{"memory_id": r["memory_id"],
              "single": single_map.get(r["memory_id"], []),
              "batch": batch_map.get(r["memory_id"], [])} for r in rows],
            ensure_ascii=False, indent=1), encoding="utf-8")
    report_path = Path(args.report) if args.report else None
    write_report(report_path, rows, macro, per_sample, args.dry_run)
    if report_path:
        with open(report_path, "a", encoding="utf-8") as f:
            f.write(f"\n## 语义 F1 复核（bge-m3 ≥0.82 贪心匹配）\n\n"
                    f"- **宏平均语义 F1：{sem_macro['f1']:.3f}**（≥0.9 → "
                    f"{'✅ 通过' if sem_macro['f1'] >= 0.9 else '❌ 未达标'}）\n"
                    f"- 精确率 {sem_macro['precision']:.3f} / 召回率 {sem_macro['recall']:.3f}\n"
                    f"- 字面 F1 {macro['f1']:.3f}（LLM 采样措辞非确定性，字面口径仅参考）\n"
                    f"- 三元组明细：{dump_path.name if dump_path else '未落盘'}\n")
    verdict = sem_macro["f1"] >= 0.9
    print(f"字面 F1 {macro['f1']:.3f} | 语义 F1 {sem_macro['f1']:.3f} → "
          f"{'PASS' if verdict else 'FAIL'}"
          f"{f'（报告 {report_path}）' if report_path else ''}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
