#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_wiki_2_0.py：知识库 2.0 回填（迁移阶段 3 的「知识库面」，零 LLM）。

做三件事（默认 dry-run，--apply 才写）：
1. 分类归一：旧 category（如 research/dsh、skill/llm）→ 7 类注册表之一；
   原始路径保留为 tag origin_category:<raw>（不丢溯源）；
2. 结构补全：page_type（entity/topic/log/index）、headline（≤30 字标题行）、
   content_hash（sha256 前 16 位）、updated_by=system:backfill；
3. 修订史首条快照：没有 wiki_revisions 的页面补一条 change_kind=create
   （9.4 #6「每页修订史首条快照，缺页必须为 0」）。

告警：未分类占比 >5% 时输出 warning（D-62 / D-47：不许静默）。

用法：

    python scripts/backfill_wiki_2_0.py --json            # 只看分布（零写入）
    python scripts/backfill_wiki_2_0.py --apply            # 落库
    python scripts/backfill_wiki_2_0.py --apply --export-unclassified tmp/wiki_unc.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme import wiki_classify  # noqa: E402
from sgme.data import db  # noqa: E402
from sgme.data import wiki_dao  # noqa: E402

UNCLASSIFIED_ALERT_RATIO = 0.05


def _load_tags(raw) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, str):
            val = json.loads(val)
        return val if isinstance(val, list) else []
    except (TypeError, ValueError):
        return []


def run(
    data_dir=None,
    *,
    apply: bool = False,
    export_unclassified: str | None = None,
    migrate_category: bool = False,
) -> dict:
    """回填知识库 2.0 结构；migrate_category=True 时把 category 改写为 7 类。

    默认**不改 category**（兼容期守 1.x 语义，D-58）：分类结果落 category_class；
    --migrate-category 是显式迁移开关（阶段 3 切读前由用户确认后执行）。
    """
    conn = db.connect_wiki(data_dir)
    try:
        rows = conn.execute(
            "SELECT page_id, title, content, category, tags, author, status, "
            "page_type, category_class, headline, content_hash FROM wiki_pages"
        ).fetchall()
        cats_before = Counter((r["category_class"] or "<null>") for r in rows)
        updates: list[tuple] = []
        cats_after: Counter = Counter()
        types_after: Counter = Counter()
        unclassified: list[str] = []
        revisions_missing = 0

        for r in rows:
            page_id = str(r["page_id"])
            content = r["content"] or ""
            raw_cat = r["category"]
            post = wiki_classify.postprocess(raw_cat, r["title"] or "", content)
            cls = post["category"]
            cats_after[cls] += 1
            types_after[post["page_type"]] += 1
            if cls == wiki_classify.UNCLASSIFIED:
                unclassified.append(page_id)
            tags = _load_tags(r["tags"])
            new_cat = raw_cat
            if migrate_category and raw_cat and raw_cat != cls:
                marker = f"origin_category:{raw_cat}"
                if marker not in tags:
                    tags.append(marker)
                new_cat = cls
            need_update = (
                r["category_class"] != cls
                or r["page_type"] != post["page_type"]
                or r["headline"] != post["headline"]
                or r["content_hash"] != wiki_dao._content_hash(content)
                or (migrate_category and raw_cat != new_cat)
            )
            if need_update:
                updates.append((
                    new_cat, cls, post["page_type"], post["headline"],
                    wiki_dao._content_hash(content),
                    "system:backfill",
                    json.dumps(tags, ensure_ascii=False) if tags else None,
                    page_id,
                ))
            if not conn.execute(
                "SELECT 1 FROM wiki_revisions WHERE page_id=? LIMIT 1", (page_id,)
            ).fetchone():
                revisions_missing += 1

        total = len(rows)
        ratio = (len(unclassified) / total) if total else 0.0
        warning = None
        if ratio > UNCLASSIFIED_ALERT_RATIO:
            warning = (
                f"未分类占比 {ratio:.1%} > {UNCLASSIFIED_ALERT_RATIO:.0%}，"
                f"请补分类规则或人工归类（{len(unclassified)} 页）"
            )

        if apply:
            wiki_dao.ensure_categories(conn)
            for item in updates:
                (new_cat, cls, ptype, head, chash, updater, tags_json, page_id) = item
                conn.execute(
                    """
                    UPDATE wiki_pages SET category=?, category_class=?, page_type=?,
                           headline=?, content_hash=?, updated_by=?, tags=?
                    WHERE page_id=?
                    """,
                    (new_cat, cls, ptype, head, chash, updater, tags_json, page_id),
                )
            if revisions_missing:
                for r in rows:
                    if conn.execute(
                        "SELECT 1 FROM wiki_revisions WHERE page_id=? LIMIT 1",
                        (r["page_id"],),
                    ).fetchone():
                        continue
                    wiki_dao.add_revision(
                        conn, str(r["page_id"]), title=r["title"], content=r["content"] or "",
                        change_kind="create",
                        author=(r["author"] or "system:backfill"),
                        summary="迁移批次首条快照（知识库 2.0 回填）",
                    )
            conn.commit()

        if export_unclassified:
            Path(export_unclassified).parent.mkdir(parents=True, exist_ok=True)
            Path(export_unclassified).write_text(
                json.dumps(unclassified, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        return {
            "total_pages": total,
            "would_update": len(updates),
            "applied": bool(apply),
            "migrate_category": bool(migrate_category),
            "categories_before": dict(cats_before.most_common(12)),
            "categories_after": dict(cats_after.most_common(12)),
            "page_types_after": dict(types_after),
            "unclassified": len(unclassified),
            "unclassified_ratio": round(ratio, 4),
            "revisions_missing": revisions_missing,
            "warning": warning,
            "export_unclassified": export_unclassified,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="知识库 2.0 回填（默认 dry-run）")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--migrate-category", action="store_true",
                    help="显式把 category 改写为 7 类（默认只写 category_class，守 1.x 兼容）")
    ap.add_argument("--export-unclassified", default=None,
                    help="导出未分类 page_id 清单（JSON）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = run(
        args.data_dir, apply=args.apply,
        export_unclassified=args.export_unclassified,
        migrate_category=args.migrate_category,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"pages={result['total_pages']} would_update={result['would_update']} "
            f"applied={result['applied']} revisions_missing={result['revisions_missing']}"
        )
        print("分类前: " + json.dumps(result["categories_before"], ensure_ascii=False))
        print("分类后: " + json.dumps(result["categories_after"], ensure_ascii=False))
        print(
            f"未分类: {result['unclassified']} 页 "
            f"({result['unclassified_ratio']:.1%})"
        )
        if result["warning"]:
            print("[WARN] " + result["warning"])
        if not args.apply:
            print("提示：dry-run（零写入）；加 --apply 才回填。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
