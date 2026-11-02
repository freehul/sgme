# -*- coding: utf-8 -*-
"""scripts/consume_skill_candidates.py：D-67 存量 skill_candidate 消费脚本。

用途
----
把 ``memories.route='skill_candidate'`` 的存量记忆分批转入 skills.db 的
``skill_candidates`` 候选池。**不写正式技能库**——默认只收高置信项（accepted），
低置信留在「待消费列表」等人工/LLM 审核；加 ``--include-low-confidence`` 才会
把低置信项也以 pending 状态收进池。

用法
----
    python scripts/consume_skill_candidates.py --dry-run
    python scripts/consume_skill_candidates.py --apply --batch-size 200
    python scripts/consume_skill_candidates.py --apply --limit 100 --min-confidence 0.9

数据目录默认取 ``sgme.config.DATA_DIR``（生产由 SGME_HOME 决定），也可用
``--data-dir`` 显式指定——脚本内不写死任何本机路径/IP。

幂等
----
memory_id 在候选池唯一：重复执行只会计入 skipped，不会产生第二行，也不会
覆盖已审核状态。每批 commit，进程中断后重跑可续。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 允许直接 `python scripts/xxx.py` 时从仓库根导入 sgme
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme import config as sgme_config  # noqa: E402
from sgme.data import db as db_mod  # noqa: E402
from sgme.data import memory_dao, skill_candidates_dao  # noqa: E402
from sgme.operations import skill_candidates as sc  # noqa: E402


def _connect(data_dir: str | Path | None, *, create_skills: bool = True):
    """打开 memory.db / skills.db（data_dir 缺省走配置，不写死路径）。

    dry-run 且 skills.db 不存在时不创建它——保证 `--dry-run` 对数据目录零写入
    （此时视为候选池为空，所有 route 记忆都算待消费）。
    """
    d = Path(data_dir) if data_dir else sgme_config.DATA_DIR
    if not (d / "memory.db").exists():
        raise SystemExit(f"memory.db 不存在: {d}（用 --data-dir 指向正确目录）")
    mem = db_mod.connect_memory(d)
    skills = db_mod.connect_skills(d) if (create_skills or (d / "skills.db").exists()) else None
    return mem, skills


def run(
    *,
    apply: bool = False,
    data_dir: str | Path | None = None,
    batch_size: int = 200,
    limit: int | None = None,
    min_confidence: float = sc.CONFIDENCE_THRESHOLD,
    include_low_confidence: bool = False,
) -> dict[str, Any]:
    """执行消费扫描；apply=False 时只统计不落库。

    Args:
        apply: True 落库，False 纯 dry-run。
        data_dir: 数据目录（含 memory.db / skills.db）。
        batch_size: 每批读取/提交条数（防一次读全表）。
        limit: 本次最多扫描多少条（None=全部）。
        min_confidence: 达到该分数且 hard gate 通过才自动 accepted。
        include_low_confidence: 默认 False——低置信记忆**不自动转入**，
            留在「待消费列表」等人工/LLM 审核；True 时才以 pending 状态收进池。

    Returns:
        统计字典（测试与运维报告共用）。
    """
    if batch_size < 1:
        raise SystemExit("--batch-size 必须 ≥ 1")
    if limit is not None and limit < 0:
        raise SystemExit("--limit 必须 ≥ 0")

    mem_conn, skills_conn = _connect(data_dir, create_skills=apply)
    stats: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "scanned": 0,
        "inserted": 0,
        "skipped": 0,
        "high_confidence": 0,
        "accepted": 0,
        "pending": 0,
        "ignored": 0,
        "duplicate": 0,
        "deferred": 0,
        "errors": 0,
        "route_total": memory_dao.count_memories_by_route(mem_conn, "skill_candidate"),
    }
    seen_sha: set[str] = set()
    cursor: str | None = None
    stop = False

    try:
        while not stop:
            take = batch_size
            if limit is not None:
                remain = limit - stats["scanned"]
                if remain <= 0:
                    break
                take = min(take, remain)
            batch = memory_dao.list_memories_by_route(
                mem_conn, "skill_candidate", limit=take, after_memory_id=cursor
            )
            if not batch:
                break
            cursor = batch[-1]["memory_id"]
            for memory in batch:
                stats["scanned"] += 1
                mid = memory["memory_id"]
                existing = (
                    skill_candidates_dao.get_candidate(skills_conn, mid)
                    if skills_conn is not None
                    else None
                )
                if existing is not None:
                    stats["skipped"] += 1
                    continue
                scored = sc.score_candidate(memory)
                draft = sc.build_skill_draft(memory)
                high = scored["high_confidence"] and scored["confidence"] >= min_confidence
                if high:
                    stats["high_confidence"] += 1
                    status = "accepted"
                elif include_low_confidence:
                    status = "pending"
                else:
                    # 默认不把低置信记忆灌进候选池：留在待消费列表等人工/LLM 审核
                    stats["deferred"] += 1
                    continue
                duplicate = draft["content_sha256"] in seen_sha
                if not duplicate and draft["content_sha256"] and skills_conn is not None:
                    duplicate = (
                        skill_candidates_dao.find_by_content_sha(
                            skills_conn, draft["content_sha256"]
                        )
                        is not None
                    )
                if not apply:
                    # dry-run：只记录本次已见指纹，保证重复内容统计准确
                    if draft["content_sha256"]:
                        seen_sha.add(draft["content_sha256"])
                    if duplicate:
                        stats["duplicate"] += 1
                        stats["ignored"] += 1
                    elif status == "accepted":
                        stats["accepted"] += 1
                    else:
                        stats["pending"] += 1
                    continue

                result = sc.adopt_skill_candidate(
                    mem_conn,
                    skills_conn,
                    mid,
                    status=status,
                    commit=False,
                )
                if not result.ok:
                    stats["errors"] += 1
                    continue
                data = result.data or {}
                stats["inserted"] += 1 if data.get("created") else 0
                final_status = data.get("status")
                if data.get("duplicate"):
                    stats["duplicate"] += 1
                if final_status == "accepted":
                    stats["accepted"] += 1
                elif final_status == "ignored":
                    stats["ignored"] += 1
                else:
                    stats["pending"] += 1
                if draft["content_sha256"]:
                    seen_sha.add(draft["content_sha256"])

            if apply:
                # 每批提交：中断可续，且不把 3000+ 行事务压在内存/日志里
                assert skills_conn is not None
                skills_conn.commit()
            if len(batch) < take:
                break
            if limit is not None and stats["scanned"] >= limit:
                stop = True
    finally:
        mem_conn.close()
        if skills_conn is not None:
            skills_conn.close()

    stats["candidate_total"] = stats["accepted"] + stats["pending"] + stats["ignored"]
    return stats


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把 route=skill_candidate 记忆分批转入技能候选池（不写正式技能库）"
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只统计不落库（默认）")
    mode.add_argument("--apply", action="store_true", help="实际写入候选池（分批复提交）")
    p.add_argument("--data-dir", default=None, help="数据目录（默认 sgme.config.DATA_DIR）")
    p.add_argument("--batch-size", type=int, default=200, help="每批读取/提交条数（默认 200）")
    p.add_argument("--limit", type=int, default=None, help="本次最多扫描条数（默认全部）")
    p.add_argument(
        "--min-confidence",
        type=float,
        default=sc.CONFIDENCE_THRESHOLD,
        help=f"自动 accepted 的最低置信度（默认 {sc.CONFIDENCE_THRESHOLD}）",
    )
    p.add_argument(
        "--include-low-confidence",
        action="store_true",
        help="把未达标的记忆也以 pending 状态收进候选池（默认留待审核，不落库）",
    )
    return p


def main(argv: list[str] | None = None) -> dict[str, Any]:
    """CLI 入口；返回统计字典（测试直接调用）。"""
    args = build_parser().parse_args(argv)
    stats = run(
        apply=bool(args.apply),
        data_dir=args.data_dir,
        batch_size=args.batch_size,
        limit=args.limit,
        min_confidence=args.min_confidence,
        include_low_confidence=bool(args.include_low_confidence),
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


if __name__ == "__main__":
    main()
