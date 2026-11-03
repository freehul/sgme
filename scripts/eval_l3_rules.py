# -*- coding: utf-8 -*-
"""eval_l3_rules.py：L3 规则层（D-28）离线评估——决定 `l3.rules.enabled` 能不能打开。

**只读生产库，零 LLM 调用。** 为什么需要它：L3 规则层默认关闭（`enabled=False`），
意味着 2.0 的「明显重复/明显改口由规则消化、只把灰区送 LLM」这条省钱主线在现网
根本没跑。但直接打开有风险——规则误判会把不同记忆判成重复（skip）或覆盖（update），
信息不可恢复。所以先量一致率，再决定：

    规则判 update/skip 的条目中，与历史真实结果（supersession 关系）一致的比例

### 数据来源（全部只读）

`memory_archive` 保留了 1.x supersession 的历史关系：被替代的旧记忆归档时写
`superseded_by = <新记忆 id>`。于是每对 (旧, 新) 就是一条「**曾经确实发生了取代**」
的真人样本——把 (旧, 新) 喂给 `l3_rules.adjudicate`：
- 规则判 `similar`（→ 送 LLM）：安全，等于没开规则层，只是多一次候选过滤；
- 规则判 `supersede`：正确命中（我们的目标是让它命中）；
- 规则判 `store` / `skip`：**与历史结果不符**，需要人工抽查是否误判。

### 用法

    python scripts/eval_l3_rules.py --db /data/data/memory.db --limit 500
    python scripts/eval_l3_rules.py --db ... --dump-csv tmp/l3_eval.csv

### 准入条件（写进设计文档 3.3.1）

1. 抽样 ≥500 对，`supersede` 命中率 ≥25%（否则开了也没省多少），且
2. 判 `store`/`skip` 的样本人工抽查 ≥30 条，**误判率 ≤5%**（误判=把不同事判成同一件），
3. 满足 1+2 后仍先灰度：`l3.rules.enabled=true` 只对单个 agent 生效一轮，观察
   `refine_runs` 的 `l1_conflict` 调用量下降幅度与误合并告警。
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

# 允许从项目根直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.engine import l3_rules  # noqa: E402


def load_pairs(db_path: str, limit: int) -> list[dict]:
    """取历史 supersession 对：(旧记忆已归档, 新记忆仍在池)。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        missing = {"memory_archive", "memories"} - tables
        if missing:
            print(f"库中缺少表 {sorted(missing)}——请指向真正的生产 memory.db"
                  f"（脚本按只读打开，不会写库）", file=sys.stderr)
            return []
        # 维度标签要尽力带上：l3_rules 的「同维度」判据若两边都空，改口检测会恒假
        # （l15.py:1230 注释记过这个坑），supersede 命中率会被系统性压低。
        rows = conn.execute(
            """
            SELECT a.memory_id AS old_id, a.content AS old_content,
                   a.superseded_by AS new_id,
                   m.content AS new_content, m.subject AS new_subject,
                   (SELECT GROUP_CONCAT(mt.dimension_id)
                      FROM memory_tags mt WHERE mt.memory_id = a.memory_id) AS old_dims,
                   (SELECT GROUP_CONCAT(mt2.dimension_id)
                      FROM memory_tags mt2 WHERE mt2.memory_id = m.memory_id) AS new_dims
            FROM memory_archive a
            JOIN memories m ON m.memory_id = a.superseded_by
            WHERE a.superseded_by IS NOT NULL
              AND a.content <> ''
              AND m.content <> ''
            ORDER BY a.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"查询失败：{exc}", file=sys.stderr)
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def eval_pairs(pairs: list[dict]) -> dict:
    """跑规则层并统计四类判定分布。"""
    stats = {"total": 0, "supersede": 0, "store": 0, "skip": 0, "llm": 0, "error": 0}
    missing_dims = 0
    suspicious: list[dict] = []
    for p in pairs:
        stats["total"] += 1
        new_dims = [d for d in (p.get("new_dims") or "").split(",") if d]
        old_dims = [d for d in (p.get("old_dims") or "").split(",") if d]
        if not new_dims and not old_dims:
            missing_dims += 1
        try:
            decision = l3_rules.adjudicate(
                {"content": p["new_content"], "subject": p.get("new_subject"),
                 "dimensions": new_dims},
                [{"memory_id": p["old_id"], "content": p["old_content"],
                  "subject": None, "dimensions": old_dims}],
            )
        except Exception as exc:  # noqa: BLE001
            stats["error"] += 1
            print(f"[warn] adjudicate 失败 {p['new_id']}: {exc}", file=sys.stderr)
            continue
        kind = getattr(decision, "kind", "llm")
        stats[kind if kind in stats else "llm"] = stats.get(kind, 0) + 1
        if kind in ("store", "skip"):
            suspicious.append({
                "old_id": p["old_id"], "new_id": p["new_id"], "rule": kind,
                "old_content": (p["old_content"] or "")[:120],
                "new_content": (p["new_content"] or "")[:120],
                "reason": getattr(decision, "reason", "") or "",
            })
    return {"stats": stats, "suspicious": suspicious, "missing_dims": missing_dims}


def main() -> int:
    ap = argparse.ArgumentParser(description="L3 规则层离线评估（只读）")
    ap.add_argument("--db", required=True, help="memory.db 路径（只读打开）")
    ap.add_argument("--limit", type=int, default=500, help="抽样对数（默认 500）")
    ap.add_argument("--dump-csv", help="把不符样本导出 CSV 供人工抽查")
    args = ap.parse_args()

    pairs = load_pairs(args.db, args.limit)
    if not pairs:
        print("没有可用的 supersession 对照样本（memory_archive.superseded_by 为空？）")
        return 1

    res = eval_pairs(pairs)
    st = res["stats"]
    total = st["total"] or 1
    hit = st["supersede"] / total
    print(f"样本对数      : {st['total']}")
    print(f"规则判 supersede: {st['supersede']}（{hit:.1%}）← 正确命中")
    print(f"规则判 llm      : {st['llm']}（{st['llm'] / total:.1%}）← 安全（回退 LLM）")
    print(f"规则判 store    : {st['store']}（{st['store'] / total:.1%}）← 与历史不符，需抽查")
    print(f"规则判 skip     : {st['skip']}（{st['skip'] / total:.1%}）← 与历史不符，需抽查")
    print(f"异常            : {st['error']}")
    if res["missing_dims"]:
        pct = res["missing_dims"] / total
        print(f"⚠ 两边维度全缺    : {res['missing_dims']}（{pct:.1%}）"
              f"——这些样本的「同维度」判据恒假，命中率会被压低，结论仅供下限参考")
    print()
    print("准入条件：supersede 命中率 ≥25%，且 store/skip 抽查误判率 ≤5%")
    print(f"当前结论：{'可进入灰度' if hit >= 0.25 else '不建议开启（命中率不足）'}")

    if args.dump_csv and res["suspicious"]:
        out = Path(args.dump_csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(res["suspicious"][0].keys()))
            w.writeheader()
            w.writerows(res["suspicious"])
        print(f"\n不符样本已导出：{out}（{len(res['suspicious'])} 条，需人工抽查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
