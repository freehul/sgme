# -*- coding: utf-8 -*-
"""scripts/wiki_supersede_audit.py：A-3 同题取代链审计（T-388 R1 · 只读 dry-run）。

背景（2026-11-16 定性实测，见 docs/plans/T-388-A-3-wiki取代范围定义-20261116.md）
----
- 本库取代结构 = 同题链为主 + 无后继批量下线；upsert 写入路径已在建同题链，
  本工具的增量是**审计与补强**（报候选，人工确认后才能改），绝不自动写链。
- ⚠️ ``supersedes`` 指针实测语义与字段名**相反**：旧页上的指针指向**后继页**
  （=「被谁取代」），方向经 4 个独立案例时间戳验证。本脚本按此语义审计；
  改名/钉文档属实现侧后续动作，不在本工具范围。

审计项（全部只读；同题 = title 精确相等，标题含日期 token 的跨日期台账天然分属不同组）
----
| 项 | 判定 | 性质 |
|---|---|---|
| missing_pointer | superseded 页无指针 | 候选补链 |
| self_pointer_with_active_successor | 自指但同题组内存在 active 页 | 链断裂嫌疑 |
| cross_title_pointer | 指针指向不同标题的页 | 疑似错链 |
| terminus_not_active | 组内最新（ingested_at）页非 active | 链尾异常 |
| pointer_cycle | 组内指针成环 | 环异常 |
| retired_no_successor | 自指且同题无 active（C1 下线） | **正常**，只计数不入候选 |

用法::

    python scripts/wiki_supersede_audit.py --db <wiki.db>            # 审计（默认输出文本）
    python scripts/wiki_supersede_audit.py --db <wiki.db> --json     # JSON 回执

退出码：0 = 无候选（链健康）；1 = 有候选（交人工复核）；2 = 用法/路径错误。
永远 mode=ro 打开，零写入。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# 允许直接 `python scripts/xxx.py` 时从仓库根导入 sgme
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ISSUE_KINDS = (
    "missing_pointer",
    "self_pointer_with_active_successor",
    "cross_title_pointer",
    "terminus_not_active",
    "pointer_cycle",
)


def load_pages(conn: sqlite3.Connection) -> list[dict]:
    """读全量页面（只取审计所需列；mode=ro 由调用方保证）。"""
    rows = conn.execute(
        "SELECT page_id, title, status, supersedes, ingested_at FROM wiki_pages"
    ).fetchall()
    return [
        {
            "page_id": str(r["page_id"]),
            "title": str(r["title"] or ""),
            "status": str(r["status"] or ""),
            "supersedes": (str(r["supersedes"]).strip() if r["supersedes"] else ""),
            "ingested_at": str(r["ingested_at"] or ""),
        }
        for r in rows
    ]


def _walk_chain(pages_by_id: dict[str, dict], start_id: str) -> tuple[list[str], bool]:
    """沿 supersedes 指针（旧→后继）行走，返回 (途经 id, 是否成环)。"""
    seen: list[str] = []
    visited: set[str] = set()
    cur = start_id
    has_cycle = False
    while cur and cur in pages_by_id and cur not in visited:
        visited.add(cur)
        seen.append(cur)
        nxt = pages_by_id[cur]["supersedes"]
        if nxt == cur:
            has_cycle = True  # 自指（单点环，单独判型，这里只标记防死循环）
            break
        cur = nxt
    if cur and cur in visited:
        has_cycle = True  # 走到任何已访问节点（含回到起点）= 成环
    return seen, has_cycle


def audit_group(pages: list[dict], pages_by_id: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """审计一个同题组。返回 (候选清单, 正常计数行)。

    方向纪律：组内时间序一律按 ingested_at（updated_at 会因下线/append 回写漂移）。
    """
    candidates: list[dict] = []
    retired = [p for p in pages if p["supersedes"] == p["page_id"]]
    has_active = any(p["status"] == "active" for p in pages)

    # 链尾：组内 ingested_at 最新的页（active 才是健康终点）
    ordered = sorted(pages, key=lambda p: p["ingested_at"])
    terminus = ordered[-1] if ordered else None
    if terminus and terminus["status"] != "active":
        # 终点自指 = C1 无后继下线（正常）；否则链尾异常
        if terminus["supersedes"] != terminus["page_id"]:
            candidates.append({
                "kind": "terminus_not_active",
                "page_id": terminus["page_id"],
                "title": terminus["title"],
                "status": terminus["status"],
                "evidence": f"组内最新页 status={terminus['status']}（ingested_at={terminus['ingested_at']}）",
            })

    if retired and not has_active:
        # C1 整组下线（全部自指/无 active）——正常，逐页计数
        normal = [{"kind": "retired_no_successor", "page_id": p["page_id"], "title": p["title"]}
                  for p in retired]
        return candidates, normal

    for p in pages:
        ptr = p["supersedes"]
        if p["status"] != "superseded":
            continue
        if not ptr:
            candidates.append({
                "kind": "missing_pointer",
                "page_id": p["page_id"],
                "title": p["title"],
                "status": p["status"],
                "evidence": "superseded 页无 supersedes 指针（候选补链：指向组内 ingested_at 更晚的页）",
            })
        elif ptr == p["page_id"]:
            if has_active:
                candidates.append({
                    "kind": "self_pointer_with_active_successor",
                    "page_id": p["page_id"],
                    "title": p["title"],
                    "status": p["status"],
                    "evidence": "指针自指但同题组内存在 active 页（链断裂嫌疑）",
                })
            # 自指且组内无 active → C1 下线，正常，已在 retired 计数
        elif ptr not in pages_by_id:
            candidates.append({
                "kind": "cross_title_pointer",
                "page_id": p["page_id"],
                "title": p["title"],
                "status": p["status"],
                "evidence": f"指针指向不存在的页 {ptr}",
            })
        else:
            target = pages_by_id[ptr]
            if target["title"] != p["title"]:
                candidates.append({
                    "kind": "cross_title_pointer",
                    "page_id": p["page_id"],
                    "title": p["title"],
                    "status": p["status"],
                    "evidence": f"指针指向不同标题页 {ptr}（title={target['title'][:30]}）",
                })

    # 环检测（排除已单独判型的自指）：从组内任意页出发行走
    for p in pages:
        if p["supersedes"] and p["supersedes"] != p["page_id"] and p["supersedes"] in pages_by_id:
            _, cyc = _walk_chain(pages_by_id, p["page_id"])
            if cyc:
                candidates.append({
                    "kind": "pointer_cycle",
                    "page_id": p["page_id"],
                    "title": p["title"],
                    "status": p["status"],
                    "evidence": f"指针沿 {p['page_id']} → {p['supersedes']} 成环",
                })
                break  # 一组报一次即可
    return candidates, []


def run(db_path: Path) -> dict:
    """审计入口：返回回执 dict（零写入）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        pages = load_pages(conn)
    finally:
        conn.close()

    by_id = {p["page_id"]: p for p in pages}
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in pages:
        if p["title"]:
            groups[p["title"]].append(p)

    candidates: list[dict] = []
    normal: list[dict] = []
    multi_groups = 0
    for title in sorted(groups):
        grp = groups[title]
        if len(grp) < 2:
            # 单页组：superseded 且指针指向别题/缺失仍是候选（孤儿页）
            cand, norm = audit_group(grp, by_id)
        else:
            multi_groups += 1
            cand, norm = audit_group(grp, by_id)
        candidates.extend(cand)
        normal.extend(norm)

    # 指针悬空补充扫描：指针指向的 id 不在库内（任何组都会被上面的 per-page 逻辑覆盖，
    # 这里兜底统计，防漏）
    dangling = [
        p for p in pages
        if p["supersedes"] and p["supersedes"] != p["page_id"] and p["supersedes"] not in by_id
    ]

    by_kind: dict[str, int] = {}
    for c in candidates:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1

    return {
        "total_pages": len(pages),
        "active": sum(1 for p in pages if p["status"] == "active"),
        "superseded": sum(1 for p in pages if p["status"] == "superseded"),
        "same_title_groups": multi_groups,
        "retired_no_successor": sum(1 for n in normal if n["kind"] == "retired_no_successor"),
        "candidates": len(candidates),
        "by_kind": by_kind,
        "dangling_pointers": len(dangling),
        "items": candidates,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="A-3 同题取代链审计（只读 dry-run，T-388 R1）")
    ap.add_argument("--db", default=None, help="wiki.db 路径（缺省取 sgme 配置 DATA_DIR/wiki.db）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（缺省文本摘要）")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.db:
        db_path = Path(args.db)
    else:
        from sgme import config as sgme_config

        db_path = Path(sgme_config.DATA_DIR) / "wiki.db"
    if not db_path.exists():
        print(f"wiki.db 不存在: {db_path}（用 --db 显式指定）", file=sys.stderr)
        return 2

    report = run(db_path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(
            f"pages={report['total_pages']} active={report['active']} "
            f"superseded={report['superseded']} 同题组={report['same_title_groups']} "
            f"C1下线={report['retired_no_successor']} 候选={report['candidates']} "
            f"悬空指针={report['dangling_pointers']}"
        )
        print(f"by_kind: {report['by_kind']}")
        for c in report["items"][:20]:
            print(f"  [{c['kind']}] {c['page_id'][:8]} {c['title'][:30]} — {c['evidence']}")
        if len(report["items"]) > 20:
            print(f"  ...（其余 {len(report['items']) - 20} 条见 --json）")
    return 0 if report["candidates"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
