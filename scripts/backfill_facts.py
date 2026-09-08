# -*- coding: utf-8 -*-
"""scripts/backfill_facts.py：存量记忆 facts 批量回填（T-148 路径 B 批量抽取）。

只做「LLM 批量抽取 → facts JSON 产出」，不做任何数据库写入。
写库（UPDATE memories.facts_json）由主代理在 NAS 生产侧执行。

特性：
- 分批调 LLM（默认 20 条/批，--batch-size），prompt 见
  sgme/resources/prompts/facts_batch_extraction.txt
- 容错解析：坏 JSON 重试 1 次；缺 id 跳过并记录；三元组字段缺失丢弃并记录
- 断点续跑：--output 已存在的 memory_id 自动跳过（幂等可重跑）
- 只产出 facts JSON（JSONL：{memory_id, facts:[...]}），零 UPDATE

用法：
  D:/Projects/SGME/.venv/Scripts/python.exe scripts/backfill_facts.py \
      --input D:/Projects/SGME/tmp/facts_sample_50.jsonl \
      --output tmp/facts_backfill_out.jsonl \
      --batch-size 20 --model agnes-2.5-flash --api-key-env AGNESAI_API_KEY
  # --dry-run：不调 LLM，只校验输入/分批/断点续跑逻辑
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgme import config as sgme_config  # noqa: E402
from sgme.llm import provider as llm_provider  # noqa: E402
from sgme.prompts.manager import PromptStore  # noqa: E402

# 批量提示词 stage 名（manifest.yaml 已注册）
BATCH_STAGE = "facts_batch_extraction"
# 坏 JSON 重试次数（首次调用 + 1 次纠错重试）
MAX_PARSE_ATTEMPTS = 2

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", flags=re.S | re.I)


def render_memories_block(rows: list[dict]) -> str:
    """把 [{memory_id, content}] 渲染成 prompt 的记忆输入块（编号 + 正文）。"""
    # 编号直接用 memory_id（与 parse_batch_response 的 expected_ids 对齐；
    # 序号会让 LLM 回答 {"id": 1} 而 id 校验按 memory_id，导致全部跳过）
    return "\n".join(f"{r['memory_id']}. {r['content']}" for r in rows)


def build_batch_prompt(rows: list[dict], prompt_text: str) -> str:
    """用 {{memories}} 占位符渲染批量提示词。"""
    return prompt_text.replace("{{memories}}", render_memories_block(rows))


def parse_batch_response(
    text: str, expected_ids: list[str]
) -> tuple[list[dict], list[dict], list[str]]:
    """解析批量抽取响应 → (ok, dropped, errors)。

    - ok:     [{memory_id, facts:[三元组]}]——id 命中输入且 facts 合法的条目
    - dropped: 被丢弃的三元组记录 [{id, reason}]（容错：缺字段/非 dict/空值）
    - errors: 解析失败信息（坏 JSON / 顶层非数组 / 未命中任何 id）

    容错规则：
    - 剔除 ```json``` 代码围栏
    - 顶层必须是 JSON 数组；否则整批视为解析失败（交由上层重试）
    - 每条 {id, facts}：id 不在输入中或缺失 → 跳过；facts 非 list → 视为 []；
      三元组非 dict 或 subject/predicate/object 任一为空 → 丢弃并记录
    """
    text = (text or "").strip()
    if not text:
        return [], [], ["空响应"]
    fence = _FENCE_RE.search(text)
    candidate = fence.group(1) if fence else text
    candidate = candidate.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        return [], [], [f"JSON 解析失败: {e}"]
    if not isinstance(data, list):
        return [], [], ["顶层不是 JSON 数组"]

    expected = set(expected_ids)
    ok: list[dict] = []
    dropped: list[dict] = []
    matched_ids = set()
    for item in data:
        if not isinstance(item, dict) or not item.get("id"):
            # 无 id 的条目：无法对应输入，跳过（不产生 dropped 记录，避免噪音）
            continue
        mid = str(item["id"])
        if mid not in expected:
            continue
        matched_ids.add(mid)
        facts: list[dict] = []
        raw_facts = item.get("facts")
        if isinstance(raw_facts, list):
            for f in raw_facts:
                if not isinstance(f, dict):
                    dropped.append({"id": mid, "reason": "非 dict 三元组"})
                    continue
                s = str(f.get("subject") or "").strip()
                p = str(f.get("predicate") or "").strip()
                o = str(f.get("object") or "").strip()
                if not (s and p and o):
                    dropped.append({"id": mid, "reason": "subject/predicate/object 缺失或为空"})
                    continue
                facts.append({"subject": s, "predicate": p, "object": o})
        else:
            # facts 非 list：按无事实处理（不硬性报错，宁缺勿滥）
            dropped.append({"id": mid, "reason": "facts 非数组，按空处理"})
        if facts:
            ok.append({"memory_id": mid, "facts": facts})
        else:
            # 无事实也产出空数组条目，保证 id 一一对应可断点续跑
            ok.append({"memory_id": mid, "facts": []})

    # id 未被模型回传的输入
    for mid in expected_ids:
        if mid not in matched_ids:
            dropped.append({"id": mid, "reason": "模型未回传该 id"})

    if not ok and not matched_ids:
        return [], dropped, ["响应未包含任何输入 id"]
    return ok, dropped, []


def _read_existing_ids(output_path: Path) -> set[str]:
    """断点续跑：读 --output 已有 JSONL 的 memory_id 集合。"""
    keys = set()
    if not output_path.exists():
        return keys
    for line in output_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("memory_id"):
            keys.add(str(rec["memory_id"]))
    return keys


def read_input_rows(input_path: Path) -> list[dict]:
    """读输入 JSONL：每行 {memory_id, content}；坏行跳过并记录。"""
    rows: list[dict] = []
    bad = 0
    for line in input_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(rec, dict) or not rec.get("memory_id"):
            bad += 1
            continue
        rows.append({"memory_id": str(rec["memory_id"]),
                     "content": str(rec.get("content") or "")})
    if bad:
        print(f"  输入含 {bad} 条坏行，已跳过")
    return rows


def append_result(output_path: Path, rec: dict) -> None:
    """追加单条结果到输出 JSONL（原子共存：create 目录 + append）。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _build_llm_node(cfg: dict, model: str, api_key_env: str) -> dict:
    """从降级链首节点构造可调用的 provider 节点（覆盖 model / api_key_env）。"""
    nodes = cfg["chains"]["refinement"]
    node = dict(nodes[0])
    if model:
        node["model"] = model
    if api_key_env:
        node["api_key_env"] = api_key_env
    return node


def run_batch_llm(
    cfg: dict,
    node: dict,
    prompt: str,
    expected_ids: list[str],
    client=None,
    dry_run: bool = False,
) -> tuple[list[dict], list[dict], list[str], int]:
    """对一批调用批量抽取（含坏 JSON 重试 1 次）。

    返回 (ok, dropped, errors, tokens_used)。dry_run=True 时不调 LLM，直接返回空结果。
    """
    if dry_run:
        return [], [], ["dry-run：不调 LLM"], 0
    rules = cfg.get("rules", {})
    last_err: list[str] = []
    tokens = 0
    for _attempt in range(MAX_PARSE_ATTEMPTS):
        try:
            # call_openai_compatible 返回 (text, usage) 二元组（provider.py 实测口径）
            text, usage = llm_provider.call_openai_compatible(
                prompt, node, rules, client
            )
        except Exception as e:  # noqa: BLE001 —— LLM 调用失败整批交上层记录
            return [], [], [f"LLM 调用失败: {e}"], tokens
        tokens += int((usage or {}).get("total_tokens") or 0)
        ok, dropped, errors = parse_batch_response(text, expected_ids)
        # T-148 门禁实测（2026-09-08）：合法 JSON 但全部空 facts / 未命中任何 id 的
        # 「批量偷懒」响应也必须重试——50 条门禁实测 3/50 样本中招（单条法均有产出）
        if not errors and any(item.get("facts") for item in ok):
            return ok, dropped, [], tokens
        if not errors:
            errors = ["批量响应全部空 facts（疑似偷懒），重试"]
        last_err = errors
    return ok, dropped, last_err, tokens


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="存量记忆 facts 批量回填（只产 JSON，不写库）",
        epilog="示例：python scripts/backfill_facts.py --input in.jsonl --output out.jsonl",
    )
    p.add_argument("--input", required=True, help="输入 JSONL：{memory_id, content} 行")
    p.add_argument("--output", required=True, help="输出 JSONL：{memory_id, facts:[...]} 行")
    p.add_argument("--batch-size", type=int, default=20, help="每批条数（默认 20）")
    p.add_argument("--model", default="", help="LLM 模型名（覆盖降级链首节点；默认取配置）")
    p.add_argument("--api-key-env", default="", help="API Key 环境变量名（默认取配置）")
    p.add_argument("--dry-run", action="store_true", help="不调 LLM，只校验管道")
    p.add_argument("--no-throttle", action="store_true",
                   help="关闭调用层节流（默认跟随 llm.yaml 的 rules.throttle）")
    return p.parse_args()


def main(argv: list[str] | None = None) -> int:
    args = _args() if argv is None else _args_from(argv)

    prompt_store = PromptStore()
    prompt_text = prompt_store.get(BATCH_STAGE).text

    in_rows = read_input_rows(Path(args.input))
    if not in_rows:
        print("输入为空或无法解析")
        return 1
    print(f"输入 {len(in_rows)} 条记忆，批大小 {args.batch_size}")

    out_path = Path(args.output)
    done_ids = _read_existing_ids(out_path)
    todo = [r for r in in_rows if r["memory_id"] not in done_ids]
    print(f"已存在 {len(done_ids)} 条（断点续跑跳过），待处理 {len(todo)} 条")

    if not todo:
        print("无待处理条目")
        return 0

    if not args.dry_run:
        cfg = sgme_config.load_llm_config()
        node = _build_llm_node(cfg, args.model, args.api_key_env)
        if args.no_throttle:
            cfg.setdefault("rules", {})["throttle"] = {"enabled": False}
        provider_name = node.get("api_key_env") or "(缺省 key env)"
        print(f"LLM provider={node.get('provider')} model={node['model']} key_env={provider_name}")
    else:
        cfg, node = None, None

    total_ok = total_drop = 0
    tokens_total = 0
    batches = [todo[i:i + args.batch_size] for i in range(0, len(todo), args.batch_size)]
    for bi, batch in enumerate(batches, 1):
        prompt = build_batch_prompt(batch, prompt_text)
        ids = [r["memory_id"] for r in batch]
        ok, dropped, errors, tokens = run_batch_llm(
            cfg, node, prompt, ids, client=None, dry_run=args.dry_run,
        )
        tokens_total += tokens
        for rec in ok:
            append_result(out_path, rec)
        for d in dropped:
            total_drop += 1
            print(f"  [drop] id={d['id']}: {d['reason']}")
        for e in errors:
            print(f"  [error] 批 {bi}: {e}")
        total_ok += len(ok)
        print(f"  批 {bi}/{len(batches)}: 完成 {len(ok)}/{len(batch)}，累计 tokens ~{tokens_total}")

    print(f"全部完成：成功 {total_ok} 条，丢弃/异常 {total_drop} 条，共消耗 tokens ~{tokens_total}")
    print(f"结果已写入 {out_path}")
    return 0


def _args_from(argv: list[str]) -> argparse.Namespace:
    """供测试直接传入 argv（避免 _args() 读真实 sys.argv）。"""
    import argparse as _argparse

    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--model", default="")
    p.add_argument("--api-key-env", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-throttle", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
