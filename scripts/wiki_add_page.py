#!/usr/bin/env python
"""scripts/wiki_add_page.py：直接向 wiki 知识库写入/更新页面（原样入库，不走 LLM 提炼）。

用法：
  python scripts/wiki_add_page.py --title "标题" --category research --tags a,b --file path.md
  python scripts/wiki_add_page.py --title "标题" --content "正文..."

说明：
- page_id 复用 refinery._gen_page_id（标题 slug + 内容哈希），与提炼链路产出一致
- insert_page 幂等（同 page_id 更新内容与元数据）
- FTS 触发器自动同步；init_wiki_fts 幂等兜底（确保虚拟表存在）
- 用途：手工整理的知识文档直接入库（如调研报告/分析文档），绕开 LLM 提炼的改写
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.data import db as db_mod
from sgme.data import wiki_dao
from sgme.refinery.output import _gen_page_id
from sgme.wiki import fts as wiki_fts


def main() -> None:
    ap = argparse.ArgumentParser(description="wiki 知识库直接入库（原样，不走提炼）")
    ap.add_argument("--title", required=True, help="页面标题")
    ap.add_argument("--file", help="markdown 正文文件路径")
    ap.add_argument("--content", help="正文（与 --file 二选一）")
    ap.add_argument("--category", default=None, help="分类（如 research/design）")
    ap.add_argument("--tags", default=None, help="逗号分隔标签")
    ap.add_argument("--data-dir", default=None, help="data 目录（缺省用默认）")
    args = ap.parse_args()

    if bool(args.file) == bool(args.content):
        ap.error("必须且只能提供 --file 或 --content 之一")
    content = Path(args.file).read_text(encoding="utf-8") if args.file else args.content

    page_id = _gen_page_id(args.title, content)
    conn = db_mod.connect_wiki(args.data_dir)
    wiki_dao.insert_page(
        conn,
        page_id,
        title=args.title,
        content=content,
        category=args.category,
        tags=args.tags.split(",") if args.tags else None,
        source_type="text",
    )
    wiki_fts.init_wiki_fts(conn)  # 幂等；触发器已同步，此调用确保 FTS 表存在
    db_mod.close(conn)
    print(f"OK page_id={page_id}")


if __name__ == "__main__":
    main()
