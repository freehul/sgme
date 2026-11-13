"""scripts/memory_slimming.py：存量记忆治理 dry-run 报表（v0.3 S7，2026-11-14）。

定位
----
设计 v0.3 §8「三步走」的**第二步**：只读出报表，不动任何数据。

  第一步（已完成）：新写入按 route 分流（S2），存量一条不动。
  第二步（本脚本）：dry-run 出分类报表 + 抽样，交人工判断。
  第三步（未开始）：分批迁移（每批 500 条，只做 archive，可整批回滚）。

安全边界
--------
- 数据库**只读打开**（sqlite3 URI mode=ro），任何写操作都会直接报错；
- 不删除、不改状态、不调 LLM；
- 输出到 stdout 与（可选）文件，不写库。

用法
----
    python scripts/memory_slimming.py --dry-run
    python scripts/memory_slimming.py --dry-run --db /data/data/memory.db --out report.md
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

#: 疑似「技术细节」的正文特征（只做粗筛，最终由人看样本判）
TECH_PATTERNS = [
    (re.compile(r"\b(?:pip|npm|pnpm|docker|kubectl|git|curl|ssh|scp)\b", re.I), "包管理/命令"),
    (re.compile(r"[A-Za-z]:\\\\|/(?:home|usr|var|etc|opt)/", re.I), "路径"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "IP"),
    (re.compile(r"\b(?:[A-Z_]{3,})\s*=\s*\S+"), "环境变量/配置"),
    (re.compile(r"Traceback|Exception|Error:|error code|0x[0-9a-f]{4,}", re.I), "报错栈"),
    (re.compile(r"\b\d{2,5}\s*(?:端口|port)\b|:\d{4,5}\b", re.I), "端口"),
    (re.compile(r"^\s*(?:def |class |function |import |from )\w", re.M), "代码片段"),
]

#: person 维度（与 data/stats_dao.PERSON_DIMENSIONS 同源；脚本独立跑时不 import 包）
PERSON_DIMENSIONS = (
    "identity", "family", "social", "preferences", "habits", "status", "goals", "values",
)
TECH_DIMENSIONS = ("tech_stack", "environment")


def _ro_connect(db_path: str) -> sqlite3.Connection:
    """只读连接：mode=ro 下任何写操作都会抛 sqlite3.OperationalError。"""
    p = Path(db_path)
    if not p.exists():
        raise SystemExit(f"数据库不存在: {db_path}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tech_hits(content: str) -> list[str]:
    """返回命中的技术特征名（空列表 = 不像技术细节）。"""
    text = content or ""
    return [name for pat, name in TECH_PATTERNS if pat.search(text)]


def _count_by_kind(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        for h in it["hits"]:
            out[h] = out.get(h, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def collect(conn: sqlite3.Connection) -> dict:
    """跑完全部只读统计，返回报表数据。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    route_dist = {
        str(r["k"]): int(r["c"]) for r in conn.execute(
            "SELECT COALESCE(route,'(null)') AS k, COUNT(*) AS c FROM memories GROUP BY k"
        )
    }

    def covered(dims: tuple[str, ...]) -> int:
        ph = ",".join("?" * len(dims))
        return int(conn.execute(
            f"SELECT COUNT(DISTINCT memory_id) FROM memory_tags WHERE dimension_id IN ({ph})",
            dims,
        ).fetchone()[0])

    person_n = covered(PERSON_DIMENSIONS)
    tech_n = covered(TECH_DIMENSIONS)

    suspects: list[dict] = []
    for row in conn.execute(
        "SELECT memory_id, content FROM memories WHERE COALESCE(route,'memory')='memory'"
    ):
        hits = _tech_hits(row["content"])
        if hits:
            suspects.append({
                "memory_id": row["memory_id"],
                "content": (row["content"] or "")[:120],
                "hits": hits,
            })

    dup_groups = [
        {"content_head": (r["content"] or "")[:60], "n": int(r["c"])}
        for r in conn.execute(
            "SELECT content, COUNT(*) AS c FROM memories GROUP BY content HAVING c > 1 "
            "ORDER BY c DESC LIMIT 30"
        )
    ]
    dup_rows = int(conn.execute(
        "SELECT COALESCE(SUM(c),0) FROM (SELECT COUNT(*) AS c FROM memories GROUP BY content HAVING c>1)"
    ).fetchone()[0])

    null_route_dim = {}
    for r in conn.execute(
        "SELECT d.id AS id, COUNT(DISTINCT m.memory_id) AS c "
        "FROM memories m LEFT JOIN memory_tags t ON t.memory_id=m.memory_id "
        "LEFT JOIN dimension_registry d ON d.id=t.dimension_id "
        "WHERE m.route IS NULL GROUP BY d.id ORDER BY c DESC"
    ):
        if r["id"]:
            null_route_dim[str(r["id"])] = int(r["c"])

    return {
        "generated_at": now,
        "total": total,
        "route_dist": route_dist,
        "person_covered": person_n,
        "person_ratio": round(person_n / total, 4) if total else 0.0,
        "tech_covered": tech_n,
        "tech_ratio": round(tech_n / total, 4) if total else 0.0,
        "suspect_tech_in_memory": {
            "count": len(suspects),
            "by_kind": _count_by_kind(suspects),
        },
        "duplicates": {"groups": len(dup_groups), "rows_in_groups": dup_rows, "top": dup_groups},
        "null_route_by_dimension": null_route_dim,
        "_suspects": suspects,
    }


def render_markdown(data: dict, samples: int) -> str:
    """生成人读的报表（Markdown）。"""
    d = data
    lines: list[str] = [
        "# 存量记忆治理 dry-run 报表",
        "",
        f"> 生成时间：{d['generated_at']}　·　**只读**：未修改任何数据",
        "",
        "## 1 总量与人/技术占比",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 记忆总数 | {d['total']} |",
        f"| 覆盖 person 维度的记忆 | {d['person_covered']}（{d['person_ratio']:.1%}） |",
        f"| 覆盖 tech 维度的记忆 | {d['tech_covered']}（{d['tech_ratio']:.1%}） |",
        "",
        "## 2 按 route 分布（决定各自去向）",
        "",
        "| route | 条数 | 建议处置 |",
        "|---|---|---|",
    ]
    suggestion = {
        "skill_candidate": "转技能候选池（S2 通道已有）→ 成功后 archive",
        "wiki_candidate": "转知识库草稿（S4 通道已有）→ 成功后 archive",
        "memory": "保留；其中疑似技术细节的见 §3",
        "(null)": "1.x 存量，按维度粗猜 route，低置信进 inbox",
        "discard": "已标记丢弃，可直接 archive",
    }
    for k, v in sorted(d["route_dist"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} | {suggestion.get(k, '待定')} |")

    lines += [
        "",
        "## 3 route=memory 里的疑似技术细节",
        "",
        f"命中 {d['suspect_tech_in_memory']['count']} 条。特征分布：",
        "",
        "| 特征 | 条数 |",
        "|---|---|",
    ]
    kinds = d["suspect_tech_in_memory"]["by_kind"]
    if kinds:
        for k, v in kinds.items():
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("| （无） | 0 |")

    suspects = d.get("_suspects") or []
    if suspects:
        lines += ["", f"抽样（前 {min(samples, len(suspects))} 条）：", "", "| memory_id | 命中 | 正文片段 |", "|---|---|---|"]
        for s in suspects[:samples]:
            body = (s["content"] or "").replace("|", "\\|")
            lines.append(f"| {s['memory_id'][:8]} | {','.join(s['hits'])} | {body} |")

    lines += [
        "",
        "## 4 重复正文",
        "",
        f"重复组 {d['duplicates']['groups']} 个，涉及 {d['duplicates']['rows_in_groups']} 条。",
    ]
    if d["duplicates"]["top"]:
        lines += ["", "| 正文开头 | 重复次数 |", "|---|---|"]
        for g in d["duplicates"]["top"][:10]:
            head = (g["content_head"] or "").replace("|", "\\|")
            lines.append(f"| {head} | {g['n']} |")

    lines += ["", "## 5 route=NULL 的维度分布（粗猜落点）", "", "| 维度 | 条数 |", "|---|---|"]
    nulls = d["null_route_by_dimension"]
    if nulls:
        for k, v in list(nulls.items())[:15]:
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("| （无） | 0 |")

    lines += [
        "",
        "## 6 下一步（本脚本不做）",
        "",
        "1. 人工确认上面的抽样与分组口径；",
        "2. 分批迁移（每批 500 条，只 archive 不删除，可整批回滚）；",
        "3. 迁移成功后才考虑开 search.routing.enabled（检索侧过滤）。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="存量记忆治理 dry-run（只读）")
    ap.add_argument("--dry-run", action="store_true", help="必须显式指定（本脚本只支持 dry-run）")
    ap.add_argument("--db", default=None, help="memory.db 路径（缺省 <项目>/data/memory.db）")
    ap.add_argument("--out", default=None, help="Markdown 报表输出路径")
    ap.add_argument("--json", dest="json_out", default=None, help="JSON 原始数据输出路径")
    ap.add_argument("--samples", type=int, default=5, help="每类抽样条数（默认 5）")
    args = ap.parse_args(argv)

    if not args.dry_run:
        print("本脚本只支持 --dry-run（只读）。真迁移由另一批任务实现。", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[1]
    db_path = args.db or str(root / "data" / "memory.db")
    conn = _ro_connect(db_path)
    try:
        data = collect(conn)
    finally:
        conn.close()

    md = render_markdown(data, args.samples)
    print(md)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"[已写入] {args.out}", file=sys.stderr)
    if args.json_out:
        public = {k: v for k, v in data.items() if not k.startswith("_")}
        Path(args.json_out).write_text(
            json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[已写入] {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
