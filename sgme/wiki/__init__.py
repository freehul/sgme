# -*- coding: utf-8 -*-
"""sgme/wiki/__init__.py：wiki 知识库扩展模块（v0.7 §10）。

扩展模块（wiki.enabled=true 时生效，禁用时核心功能零影响）。
- fts.py：wiki_fts 索引初始化 + BM25 检索
- routes.py：/v1/wiki/* 端点（ingest 对接 refinery）
- raw_store.py：原件归档目录管理（§8.5）
"""
from __future__ import annotations

from sgme.wiki.fts import init_wiki_fts, search_wiki_fts

__all__ = ["init_wiki_fts", "search_wiki_fts"]
