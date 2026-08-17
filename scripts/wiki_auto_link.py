#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/wiki_auto_link.py：wiki 页面自动关联（2026-08-18 用户需求）。

- 标题引用（references）：页 A 的 content 包含页 B 的 title → A references B
- 共享标签（related）：两页 tags 有交集 → related 链
- 复用 wiki_dao.insert_link（幂等，同三元组不重复；rel_type 校验注册表）
- 默认 dry-run（只统计）；--apply 才写入

用法：
  python scripts/wiki_auto_link.py [--db PATH] [--apply] [--min-title-len N] [--link-all-tags]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.data import wiki_dao

DEFAULT_DB = "data/wiki.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="wiki 页面自动关联")
    parser.add_argument("--db", default=DEFAULT_DB, help="wiki.db 路径（默认 data/wiki.db）")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    parser.add_argument("--min-title-len", type=int, default=4, help="标题引用最小标题长度（防噪声）")
    parser.add_argument("--max-tag-size", type=int, default=15,
                        help="共享标签建链的同 tag 页面数上限（防通用大 tag 爆炸；超过则跳过该 tag）")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    pages = conn.execute(
        "SELECT page_id, title, content, tags FROM wiki_pages WHERE status='active' OR status IS NULL"
    ).fetchall()
    print(f"[wiki-link] 页面: {len(pages)} 个")

    # 标题索引（title → page_id；长度过滤 + 去重保首）
    title_map: dict[str, str] = {}
    for p in pages:
        t = (p["title"] or "").strip()
        if len(t) >= args.min_title_len and t not in title_map:
            title_map[t] = p["page_id"]

    ref_links = 0
    related_links = 0
    candidates: list[tuple[str, str, str, float]] = []

    # 1) 标题引用：A.content 包含 B.title → A references B
    for p in pages:
        content = p["content"] or ""
        for title, target_id in title_map.items():
            if title in content and target_id != p["page_id"]:
                candidates.append((p["page_id"], target_id, "references", 0.9))
                ref_links += 1

    # 2) 共享标签：tags 交集 → related
    tag_map: dict[str, list[str]] = {}
    for p in pages:
        for tag in (p["tags"] or "").split(","):
            tag = tag.strip()
            if tag:
                tag_map.setdefault(tag, []).append(p["page_id"])
    seen_pairs: set[tuple[str, str]] = set()
    skipped_tags = 0
    for tag, ids in tag_map.items():
        if len(ids) < 2:
            continue
        if len(ids) > args.max_tag_size:
            # 通用大 tag（如 sgme、ai）会产生 O(n^2) 噪声链，跳过
            skipped_tags += 1
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                pair = (a, b) if a < b else (b, a)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                candidates.append((pair[0], pair[1], "related", 0.6))
                related_links += 1

    print(f"[wiki-link] 标题引用: {ref_links} | 共享标签: {related_links} | 跳过通用 tag: {skipped_tags} | 合计: {len(candidates)}")

    if not args.apply:
        print("[wiki-link] dry-run，未写入。加 --apply 执行")
        conn.close()
        return 0

    written = 0
    for source_id, target_id, rel_type, conf in candidates:
        try:
            wiki_dao.insert_link(conn, source_id, target_id, rel_type, confidence=conf, source="auto")
            written += 1
        except ValueError as e:
            print(f"  跳过 {rel_type}: {e}")
    print(f"[wiki-link] 写入完成: {written} 条")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
