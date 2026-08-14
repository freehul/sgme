# -*- coding: utf-8 -*-
"""一次性数据修复：wiki_pages.tags 双重 JSON 编码归一化（2026-08-14）。

背景：部分 wiki 页面 tags 被存成双重编码（外层是 JSON 字符串、内容又是
JSON 数组字符串），如 ``'"[\\"a\\", \\"b\\"]"'``，导致前端 v-for 逐字符渲染。
sgme/data/wiki_dao._parse_tags 已在读取层防御（loads 两次）；
本脚本把库里存量脏数据归一化为标准单层 JSON 数组，一劳永逸。
幂等：只对「第一层 loads 得到 str 且再解一层得 list」的行做修复。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "wiki.db"


def normalize(raw: str | None) -> str | None:
    """把任意 tags 存值归一化为标准 JSON 数组字符串；None/非法 → None。"""
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (TypeError, ValueError):
            return None
    if not isinstance(val, list):
        return None
    return json.dumps(val, ensure_ascii=False)


def main() -> int:
    if not DB.exists():
        print(f"未找到 wiki.db: {DB}")
        return 1
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT page_id, title, tags FROM wiki_pages").fetchall()
    fixed = 0
    for r in rows:
        new = normalize(r["tags"])
        if new is None or new == r["tags"]:
            continue
        conn.execute("UPDATE wiki_pages SET tags=? WHERE page_id=?", (new, r["page_id"]))
        fixed += 1
        print(f"修复: {r['title']}  tags={r['tags']!r} -> {new!r}")
    conn.commit()
    conn.close()
    print(f"共修复 {fixed} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())