#!/usr/bin/env python
"""把生产库的人工纠错清单迁移到新库（切库后必做）。

背景（2026-11-01 全量重建）：生产库 ``status='rejected'`` 的 10,308 条里，
9,693 条是 supersession 联动（旧值被新值替代，系统行为），**615 条是人工纠错**
（用户明确纠正过的事实，如「透析改为每周一三五」「没有女儿」）。

2.0 重建只读原件、不知道事后被纠正过，因此这些旧说法会被重新提炼出来。
不迁移这份清单，切库即是倒退。

匹配策略（保守，宁可漏不可错杀）：
  1. 归一化后**完全相同** → 自动置 rejected（reason 带来源标记）
  2. 字符 3-gram Jaccard >= 阈值（默认 0.85）→ 只进候选清单，**不自动改**
     （相似不等于同一件事，误杀真实记忆的代价高于漏放一条旧说法）

用法：
  # 先看会改哪些（默认 dry-run）
  python scripts/migrate_rejected_manual.py --db <path>/memory.db --input <json>

  # 确认后落库
  python scripts/migrate_rejected_manual.py --db <path>/memory.db --input <json> --apply

输入 JSON 结构：[{"content": str, "reason": str, "rejected_at": str}, ...]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REASON_PREFIX = "人工纠错迁移-20261101"


def normalize(text: str) -> str:
    """归一化用于精确比对：去空白、统一全半角括号与引号、去结尾标点。"""
    s = (text or "").strip()
    table = str.maketrans({"（": "(", "）": ")", "“": '"', "”": '"', "‘": "'", "’": "'"})
    s = s.translate(table)
    s = re.sub(r"\s+", "", s)
    return s.rstrip("。．.!！?？;；,，")


def grams(text: str, n: int = 3) -> set[str]:
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移人工纠错清单到新库")
    ap.add_argument("--db", required=True, help="目标 memory.db 路径")
    ap.add_argument("--input", required=True, help="人工纠错清单 JSON（prod_rejected_manual.json）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--fuzzy-threshold", type=float, default=0.85, help="模糊候选阈值（默认 0.85）")
    ap.add_argument("--fuzzy-report", default="", help="模糊候选清单输出路径（JSON）")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"目标库不存在：{db}")
        return 2
    manual = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(f"人工纠错清单：{len(manual)} 条 → 目标库 {db}")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    active = conn.execute(
        "SELECT memory_id, content FROM memories WHERE status='active'"
    ).fetchall()
    by_norm: dict[str, list[sqlite3.Row]] = {}
    for row in active:
        by_norm.setdefault(normalize(row["content"]), []).append(row)
    print(f"目标库 active 记忆：{len(active)} 条")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    exact: list[tuple[str, str]] = []          # (memory_id, 原纠错理由)
    fuzzy_candidates: list[dict] = []
    matched_norms: set[str] = set()

    # 索引：尚未被精确命中的 active 内容，供模糊比对
    remaining = [(r["memory_id"], r["content"]) for r in active]

    for item in manual:
        content = item.get("content", "")
        reason = item.get("reason") or ""
        key = normalize(content)
        if not key:
            continue
        hits = by_norm.get(key)
        if hits:
            matched_norms.add(key)
            for row in hits:
                exact.append((row["memory_id"], reason))
            continue
        # 模糊候选
        g = grams(key)
        if not g:
            continue
        best_id, best_score = None, 0.0
        for mid, mcontent in remaining:
            score = jaccard(g, grams(normalize(mcontent)))
            if score > best_score:
                best_id, best_score = mid, score
        if best_id and best_score >= args.fuzzy_threshold:
            fuzzy_candidates.append(
                {
                    "memory_id": best_id,
                    "score": round(best_score, 3),
                    "correction": content[:120],
                    "reason": reason[:200],
                }
            )

    # 去重（同一 memory_id 命中多条纠错只保留一次）
    seen: set[str] = set()
    exact_rows: list[tuple[str, str]] = []
    for mid, reason in exact:
        if mid in seen:
            continue
        seen.add(mid)
        exact_rows.append((mid, reason))

    print(f"\n精确命中：{len(exact_rows)} 条（将置 rejected）")
    print(f"模糊候选：{len(fuzzy_candidates)} 条（仅供参考，不自动改）")

    for mid, reason in exact_rows[:20]:
        row = conn.execute("SELECT content FROM memories WHERE memory_id=?", (mid,)).fetchone()
        print(f"  · {mid[:8]} {(row['content'] if row else '')[:60]}  ← {reason[:40]}")
    if len(exact_rows) > 20:
        print(f"  …… 其余 {len(exact_rows) - 20} 条")

    if fuzzy_candidates:
        out = Path(args.fuzzy_report) if args.fuzzy_report else db.parent.parent / "rejected_fuzzy_candidates.json"
        out.write_text(json.dumps(fuzzy_candidates, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n模糊候选清单已写入：{out}")
        for c in fuzzy_candidates[:10]:
            print(f"  ~ {c['score']} {c['correction'][:50]}")

    if not args.apply:
        print("\n[试运行] 未写库。确认无误后加 --apply")
        conn.close()
        return 0

    if exact_rows:
        conn.executemany(
            "UPDATE memories SET status='rejected', rejected_at=?, "
            "reject_reason=? WHERE memory_id=? AND status='active'",
            [(now, f"{REASON_PREFIX}-{reason[:180]}" or REASON_PREFIX, mid) for mid, reason in exact_rows],
        )
        conn.commit()
    print(f"\n已置 rejected：{len(exact_rows)} 条")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
