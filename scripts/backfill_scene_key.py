#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_scene_key.py：D-53 场景键回填（迁移阶段 3 规则回填的一部分，零 LLM）。

规则见设计 §3.0.2：scene_key = 主维度（priority 最小，并列按 id 字典序）: 来源类
（agent_tag → coding/desktop/mobile/other）。缺主维度或缺 agent_tag → unknown（不猜）。

用法：

    # 1) dry-run（只统计，不写库）
    python scripts/backfill_scene_key.py

    # 2) 回填 scene_key 为空的行（幂等，可重跑）
    python scripts/backfill_scene_key.py --apply

    # 3) 全量重算（规则变更后）
    python scripts/backfill_scene_key.py --apply --all

    # 4) 导出 unknown 清单（9.4 #8 要求可导出）
    python scripts/backfill_scene_key.py --export-unknown tmp/scene_key_unknown.json

纪律：只写 memories.scene_key；不动机器标签、不改 updated_at；不调 LLM。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.data import db  # noqa: E402
from sgme.data import scene_key as sk  # noqa: E402


def _rows(conn, only_empty: bool = True):
    where = "WHERE m.status='active'"
    if only_empty:
        where += " AND (m.scene_key IS NULL OR m.scene_key='')"
    return conn.execute(
        f"""
        SELECT m.memory_id, m.agent_tag, GROUP_CONCAT(t.dimension_id) AS dims
        FROM memories m
        LEFT JOIN memory_tags t ON t.memory_id = m.memory_id
        {where}
        GROUP BY m.memory_id
        """
    ).fetchall()


def run(
    data_dir=None,
    *,
    apply: bool = False,
    only_empty: bool = True,
    export_unknown: str | None = None,
) -> dict:
    conn = db.connect_memory(data_dir)
    try:
        priorities = sk.load_priorities(conn)
        rows = _rows(conn, only_empty)
        updates: list[tuple[str, str]] = []
        dist: Counter = Counter()
        unknown_no_tag = 0
        unknown_no_agent = 0
        for r in rows:
            dims = (r["dims"] or "").split(",") if r["dims"] else []
            key = sk.build_scene_key(dims, r["agent_tag"], priorities)
            updates.append((key, str(r["memory_id"])))
            dist[key] += 1
            if key == sk.UNKNOWN:
                if not dims:
                    unknown_no_tag += 1
                elif not (r["agent_tag"] or "").strip():
                    unknown_no_agent += 1
        if apply and updates:
            conn.executemany(
                "UPDATE memories SET scene_key=? WHERE memory_id=?", updates
            )
            conn.commit()
        if export_unknown:
            unknown_ids = [mid for key, mid in updates if key == sk.UNKNOWN]
            Path(export_unknown).parent.mkdir(parents=True, exist_ok=True)
            Path(export_unknown).write_text(
                json.dumps(unknown_ids, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return {
            "candidates": len(rows),
            "applied": bool(apply),
            "only_empty": only_empty,
            "distribution": dict(dist.most_common(20)),
            "unknown": dist.get(sk.UNKNOWN, 0),
            "unknown_no_tag": unknown_no_tag,
            "unknown_no_agent": unknown_no_agent,
            "export_unknown": export_unknown,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-53 scene_key 回填（默认 dry-run）")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--all", action="store_true", help="全量重算（默认只补空的）")
    ap.add_argument("--export-unknown", default=None, help="导出 unknown 记忆 id 清单（JSON）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = run(
        args.data_dir,
        apply=args.apply,
        only_empty=not args.all,
        export_unknown=args.export_unknown,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"候选 {result['candidates']} 条 applied={result['applied']} "
            f"unknown={result['unknown']}（无标签 {result['unknown_no_tag']} / "
            f"无 agent_tag {result['unknown_no_agent']}）"
        )
        print("分布 top20: " + json.dumps(result["distribution"], ensure_ascii=False))
        if not args.apply:
            print("提示：dry-run（零写入）；加 --apply 才回填。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
