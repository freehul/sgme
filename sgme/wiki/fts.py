# -*- coding: utf-8 -*-
"""sgme/wiki/fts.py：wiki_pages FTS5 索引与同步触发器（v0.7 §8.4，对称 scenes_fts）。

wiki 是扩展模块：wiki.enabled=true 时才初始化（server 启动挂载路由时调用）。
"""
from __future__ import annotations

import sqlite3

WIKI_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    content_seg,
    description_seg,
    page_id UNINDEXED,
    content='wiki_pages',
    content_rowid='rowid'
);
"""

WIKI_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS wiki_ai AFTER INSERT ON wiki_pages BEGIN
    INSERT INTO wiki_fts(rowid, content_seg, description_seg, page_id)
    VALUES (new.rowid, new.content_seg, new.description_seg, new.page_id);
END;
CREATE TRIGGER IF NOT EXISTS wiki_ad AFTER DELETE ON wiki_pages BEGIN
    INSERT INTO wiki_fts(wiki_fts, rowid, content_seg, description_seg, page_id)
    VALUES ('delete', old.rowid, old.content_seg, old.description_seg, old.page_id);
END;
CREATE TRIGGER IF NOT EXISTS wiki_au AFTER UPDATE ON wiki_pages BEGIN
    INSERT INTO wiki_fts(wiki_fts, rowid, content_seg, description_seg, page_id)
    VALUES ('delete', old.rowid, old.content_seg, old.description_seg, old.page_id);
    INSERT INTO wiki_fts(rowid, content_seg, description_seg, page_id)
    VALUES (new.rowid, new.content_seg, new.description_seg, new.page_id);
END;
"""


def _fts_has_description(conn: sqlite3.Connection) -> bool:
    """检测 wiki_fts 是否含 description_seg 列（FTS5 无法 ALTER 加索引列，缺则重建）。"""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(wiki_fts)").fetchall()]
        return "description_seg" in cols
    except Exception:
        return False


def _triggers_have_description(conn: sqlite3.Connection) -> bool:
    """检测同步触发器是否含 description_seg（2026-08-16 真实链路暴露的隐藏缺口：
    DROP TABLE wiki_fts 不删触发器，IF NOT EXISTS 不会覆盖旧触发器——旧触发器
    缺 description_seg 同步导致新行进索引时该列丢失，FTS 检索不到 description。"""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='wiki_ai'"
        ).fetchone()
        return bool(row and row["sql"] and "description_seg" in row["sql"])
    except Exception:
        return False


def init_wiki_fts(conn: sqlite3.Connection) -> bool:
    """初始化 wiki_fts 虚拟表与触发器（幂等，对称 init_scenes_fts）。

    结构升级（W1，2026-08-16）：老结构缺 description_seg → DROP 重建
    （FTS5 外部内容表加索引列无法 ALTER），重建后回填 content_seg/description_seg。
    中文检索不降级：content_seg 保留，description_seg 同 jieba 分词方案。

    Returns:
        True=成功；False=失败（调用方降级 BM25/LIKE 兜底，不炸服务）。
    """
    try:
        if not _fts_has_description(conn) or not _triggers_have_description(conn):
            # 表缺列或触发器缺同步字段 → 一并重建（触发器挂在 wiki_pages 上，
            # DROP TABLE 不删触发器，必须显式 DROP TRIGGER 否则旧版残留）
            conn.execute("DROP TABLE IF EXISTS wiki_fts")
            conn.execute("DROP TRIGGER IF EXISTS wiki_ai")
            conn.execute("DROP TRIGGER IF EXISTS wiki_ad")
            conn.execute("DROP TRIGGER IF EXISTS wiki_au")
        conn.executescript(WIKI_FTS_DDL)
        conn.executescript(WIKI_FTS_TRIGGERS)
        # 存量数据回填（首次建表或重建时）
        conn.execute(
            "INSERT OR IGNORE INTO wiki_fts(rowid, content_seg, description_seg, page_id)"
            " SELECT rowid, content_seg, description_seg, page_id FROM wiki_pages"
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def search_wiki_fts(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """wiki_fts BM25 检索（L0 兜底：FTS 不可用/空召回 → LIKE）。

    Returns:
        [{page_id, title, snippet}]（按 BM25 相关度降序）。
    """
    results: list[dict] = []
    try:
        # 中文查询用 OR 拆词（jieba），英文整句直接 MATCH
        terms = _split_terms(query)
        if terms:
            match_expr = " OR ".join(f'"{t}"' for t in terms)
            rows = conn.execute(
                "SELECT p.page_id, p.title, p.content, p.tags,"
                " bm25(wiki_fts) AS score"
                " FROM wiki_fts JOIN wiki_pages p ON p.rowid = wiki_fts.rowid"
                " WHERE wiki_fts MATCH ? AND p.status='active'"
                " ORDER BY score LIMIT ?",
                (match_expr, limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "page_id": r["page_id"],
                    "title": r["title"],
                    "snippet": r["content"][:200] if r["content"] else "",
                    "tags": r["tags"] or [],
                })
    except Exception:
        results = []
    # FTS 空召回或不可用 → LIKE 兜底
    if not results:
        try:
            like = f"%{query}%"
            rows = conn.execute(
                "SELECT page_id, title, content, tags FROM wiki_pages"
                " WHERE (content LIKE ? OR title LIKE ?) AND status='active'"
                " ORDER BY updated_at DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
            results = [
                {"page_id": r["page_id"], "title": r["title"],
                 "snippet": (r["content"] or "")[:200],
                 "tags": r["tags"] or []}
                for r in rows
            ]
        except Exception:
            results = []
    return results


def _split_terms(query: str) -> list[str]:
    """查询拆词：优先 jieba，失败退化为整句。"""
    try:
        import jieba

        terms = [t.strip() for t in jieba.cut(query) if t.strip()]
        return terms or [query]
    except Exception:
        return [query]
