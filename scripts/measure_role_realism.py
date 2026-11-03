# -*- coding: utf-8 -*-
"""measure_role_realism.py：D-74 角色真实感基线测量（四卡 × 固定 10 问题集）。

**为什么需要它**：`operations/role_metrics.py` 的 docstring 一直写着「与测量脚本
``measure_role_realism.py`` 逐字对齐」，但该脚本此前根本不存在——四张卡的 D-74
基线快照因此一直没有产出，指标口径也无从复核。本脚本补齐这一环。

### 它做什么

1. 取一张（或四张）角色卡，调 ``assemble()`` 拿人格层 `system_prompt`；
2. 对固定 10 个问题逐一生成回复（真实 LLM 调用，默认免费档 agnes）；
3. 四项确定性指标由**服务端** ``role_metrics.metrics_record`` 计算并落卡
   （`extensions.sgme_metrics`），脚本只负责取样本、传原文。

### 用法

    # 1) 预演：只打印将要跑什么，不调 LLM、不写卡
    python scripts/measure_role_realism.py --dry-run

    # 2) 单卡跑并写基线（首次自动设 baseline）
    python scripts/measure_role_realism.py --role butler --set-baseline

    # 3) 用自己的回复文件（避免重复花钱）：{question_index: reply}
    python scripts/measure_role_realism.py --role butler --replies tmp/butler.json

### 成本

四张卡 × 10 题 = 40 次 LLM 调用（免费档 agnes 即可）。脚本**默认不跑真实调用**，
必须显式给 ``--apply`` 或 ``--role`` 才执行；``--dry-run`` 永远零成本。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme import config as sgme_config  # noqa: E402
from sgme.care import roles as roles_mod  # noqa: E402
from sgme.operations import care as care_ops  # noqa: E402
from sgme.operations import role_metrics  # noqa: E402

# 固定 10 问题集（D-74：一套问题跑四张卡才有横向可比性；不得逐次改动）
QUESTIONS: tuple[str, ...] = (
    "今天过得怎么样？",
    "你还记得我上次说的事吗？",
    "你是谁？你是什么模型？",
    "我最近有点累",
    "帮我看看明天有什么安排",
    "你觉得我应该换工作吗？",
    "说点你自己想说的",
    "我今天不想说话",
    "你喜欢什么？",
    "我们认识多久了？",
)


def _roles_dir() -> Path:
    return Path(sgme_config.ROLES_DIR)


def _list_role_ids() -> list[str]:
    try:
        items = roles_mod.list_roles(_roles_dir())
    except Exception as exc:  # noqa: BLE001
        print(f"读取角色卡失败：{exc}", file=sys.stderr)
        return []
    out: list[str] = []
    for r in items:
        rid = r.get("role_id") or r.get("id")
        if rid:
            out.append(str(rid))
    return out


def _generate(role_id: str, cfg: dict, question: str) -> str:
    """用装配出的人格层 + 用户问题跑一次生成（返回回复原文）。"""
    from sgme.data import db as db_mod
    from sgme.llm import chain as llm_chain

    mem_conn = db_mod.connect_memory(Path(sgme_config.DATA_DIR))
    try:
        res = care_ops.assemble(role_id, mem_conn, cfg, render=True, user_name="你")
        if not res.ok or not res.data:
            raise RuntimeError(f"装配失败：{res.message}")
        system_prompt = res.data.get("system_prompt") or ""
    finally:
        mem_conn.close()
    prompt = f"{system_prompt}\n\n用户：{question}\n你的回复："
    out, _provider, _usage = llm_chain.call_with_fallback(
        cfg["llm"], prompt, chain_name="refinement", client=None,
    )
    return (out or "").strip()


def _run_role(role_id: str, cfg: dict, replies: dict[int, str] | None,
              set_baseline: bool) -> dict:
    items: list[dict] = []
    for idx, q in enumerate(QUESTIONS):
        if replies is not None and str(idx) in replies:
            reply = str(replies[str(idx)])
        else:
            print(f"  [{idx + 1}/{len(QUESTIONS)}] {q}")
            reply = _generate(role_id, cfg, q)
        items.append({"question": q, "reply": reply})
    res = role_metrics.metrics_record(
        role_id, items, set_as_baseline=set_baseline,
    )
    return {"ok": res.ok, "data": res.data, "error": res.message}


def main() -> int:
    ap = argparse.ArgumentParser(description="D-74 角色真实感基线测量")
    ap.add_argument("--role", help="只跑这一张卡（role_id）")
    ap.add_argument("--apply", action="store_true", help="对全部卡真实执行（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不调 LLM 不写卡")
    ap.add_argument("--set-baseline", action="store_true",
                    help="首次运行写 baseline 快照（已存在则只更新 latest/runs）")
    ap.add_argument("--replies", help="用已有回复 JSON 代替实时生成（省 token）")
    args = ap.parse_args()

    role_ids = [args.role] if args.role else _list_role_ids()
    if not role_ids:
        print("没有找到角色卡", file=sys.stderr)
        return 1

    cfg = sgme_config.load_config()
    print(f"角色：{', '.join(role_ids)}")
    print(f"问题集：{len(QUESTIONS)} 条（D-74 固定集，不可逐次改动）")
    print(f"预计 LLM 调用：{len(role_ids) * len(QUESTIONS)} 次"
          f"{'（使用了 --replies，实际更少）' if args.replies else ''}")
    print()

    if args.dry_run or not (args.apply or args.role):
        print("DRY-RUN：不调 LLM、不写卡。真正执行请加 --apply（或指定 --role）。")
        for q in QUESTIONS:
            print(f"  - {q}")
        return 0

    replies = None
    if args.replies:
        replies = json.loads(Path(args.replies).read_text(encoding="utf-8"))
        print(f"已载入回复文件：{args.replies}")

    failed = 0
    for rid in role_ids:
        print(f"\n=== {rid} ===")
        try:
            out = _run_role(rid, cfg, replies, args.set_baseline)
        except Exception as exc:  # noqa: BLE001
            print(f"  失败：{exc}", file=sys.stderr)
            failed += 1
            continue
        if not out["ok"]:
            print(f"  指标落卡失败：{out['error']}", file=sys.stderr)
            failed += 1
            continue
        data = out["data"] or {}
        for k in ("first_sentence_len", "exclaim_rate", "restate_rate", "ai_taste_rate"):
            if k in data:
                print(f"  {k}: {data[k]}")
        print(f"  baseline: {'已写入' if args.set_baseline else '未动'}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
