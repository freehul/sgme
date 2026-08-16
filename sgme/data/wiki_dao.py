# -*- coding: utf-8 -*-
"""sgme/data/wiki_dao.py 补齐：wiki_pages / wiki_links CRUD（v0.7 §10 wiki 扩展模块）。

v0.7 三库拆分后本模块被清空（raw_files → session_dao，scenes 系列 → scene_dao）。
阶段 3（2026-08-08）补齐 wiki 扩展专用函数，对应 WIKI_DDL 收缩后的两张表。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seg(text: str | None) -> str | None:
    """jieba 分词（对称 memory_dao/scene_dao；jieba 不可用时原样返回）。"""
    if not text:
        return None
    try:
        import jieba

        return " ".join(jieba.cut(text))
    except Exception:
        return text


# ---------- wiki_pages CRUD ----------

def insert_page(
    conn: sqlite3.Connection,
    page_id: str,
    title: str,
    content: str,
    category: str | None = None,
    tags: list[str] | None = None,
    source_type: str | None = None,
    source_url: str | None = None,
    source_file: str | None = None,
    ingested_at: str | None = None,
    description: str | None = None,
    author: str | None = None,
    status: str | None = None,
    supersedes: str | None = None,
) -> str:
    """插入 wiki 页面（按 page_id 幂等：已存在则更新内容与元数据）。

    W1 新增：description（L1 摘要，描述即索引）/ author / status / supersedes
    （多 agent 共享溯源，方案 v0.3 §5.1）。description_seg 由 _seg 计算。
    status 缺省 'active'。

    Returns:
        page_id。
    """
    now = ingested_at or _now_iso()
    conn.execute(
        """
        INSERT INTO wiki_pages (page_id, title, content, category, tags,
                                source_type, source_url, source_file,
                                ingested_at, updated_at, content_seg,
                                description, description_seg, author, status, supersedes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(page_id) DO UPDATE SET
            title=excluded.title, content=excluded.content,
            category=excluded.category, tags=excluded.tags,
            source_type=excluded.source_type, source_url=excluded.source_url,
            source_file=excluded.source_file, updated_at=excluded.updated_at,
            content_seg=excluded.content_seg,
            description=excluded.description,
            description_seg=excluded.description_seg,
            author=excluded.author,
            status=excluded.status,
            supersedes=excluded.supersedes
        """,
        (
            page_id, title, content, category,
            json.dumps(tags, ensure_ascii=False) if tags else None,
            source_type, source_url, source_file,
            now, now, _seg(content),
            description, _seg(description),
            author, status if status is not None else "active", supersedes,
        ),
    )
    conn.commit()
    return page_id


def _parse_tags(d: dict) -> dict:
    """tags 列是 JSON 字符串，解析为列表（get_page/list_pages 共用）。

    防御双重编码脏数据（2026-08-14 故障：部分页面 tags 被存成
    ``'"[\\"a\\", \\"b\\"]"'``——外层 loads 得到 str 而非 list，前端 v-for
    逐字符渲染）。规则：第一层得到 str 则再 loads 一次，仍非 list 视为空。
    """
    raw = d.get("tags")
    if not raw:
        d["tags"] = []
        return d
    try:
        val = json.loads(raw)
        if isinstance(val, str):  # 双重编码：再解一层
            val = json.loads(val)
        d["tags"] = val if isinstance(val, list) else []
    except (TypeError, ValueError):
        d["tags"] = []
    return d


def get_page(conn: sqlite3.Connection, page_id: str) -> dict | None:
    """单页查询。"""
    row = conn.execute(
        "SELECT * FROM wiki_pages WHERE page_id=?", (page_id,)
    ).fetchone()
    if row is None:
        return None
    return _parse_tags(dict(row))


def list_pages(
    conn: sqlite3.Connection,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """页面列表（按 updated_at 降序；category 可选过滤）。"""
    if category:
        rows = conn.execute(
            "SELECT * FROM wiki_pages WHERE category=? AND status='active'"
            " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (category, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM wiki_pages WHERE status='active'"
            " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [_parse_tags(dict(r)) for r in rows]


def update_page_content(
    conn: sqlite3.Connection,
    page_id: str,
    content: str,
    title: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> bool:
    """更新页面内容与可选元数据（title/category/tags/description 传 None 表示不修改）。

    W1：description 显式传入时同步重算 description_seg（API 层 PATCH 默认不动
    description，本函数仅数据层能力）。
    """
    cur = conn.execute(
        "SELECT title, category, tags, description FROM wiki_pages WHERE page_id=?",
        (page_id,),
    )
    row = cur.fetchone()
    if row is None:
        return False
    new_title = title if title is not None else row["title"]
    new_cat = category if category is not None else row["category"]
    new_tags = json.dumps(tags, ensure_ascii=False) if tags is not None else row["tags"]
    if description is not None:
        conn.execute(
            """
            UPDATE wiki_pages SET title=?, content=?, category=?, tags=?,
                   updated_at=?, content_seg=?, description=?, description_seg=?
            WHERE page_id=?
            """,
            (new_title, content, new_cat, new_tags, _now_iso(), _seg(content),
             description, _seg(description), page_id),
        )
    else:
        conn.execute(
            """
            UPDATE wiki_pages SET title=?, content=?, category=?, tags=?,
                   updated_at=?, content_seg=?
            WHERE page_id=?
            """,
            (new_title, content, new_cat, new_tags, _now_iso(), _seg(content), page_id),
        )
    conn.commit()
    return True


def delete_page(conn: sqlite3.Connection, page_id: str) -> bool:
    """删除页面（级联删除关联链接）。"""
    cur = conn.execute("SELECT 1 FROM wiki_pages WHERE page_id=?", (page_id,))
    if cur.fetchone() is None:
        return False
    conn.execute("DELETE FROM wiki_links WHERE source_id=? OR target_id=?", (page_id, page_id))
    conn.execute("DELETE FROM wiki_pages WHERE page_id=?", (page_id,))
    conn.commit()
    return True


def count_pages(conn: sqlite3.Connection) -> int:
    """页面总数。"""
    return conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]


# ---------- wiki_links ----------

def _known_rel_types() -> frozenset[str]:
    """读取关系类型注册表，返回合法 rel_type 集合（T-14）。

    注册表权威：wiki_links.rel_type 枚举由 registry/relations.yaml 定义，
    DB 不做 CHECK 约束（支持热扩展）。注册表缺失/损坏时 fail-closed 拒绝写入。
    """
    from sgme import config as config_mod

    rels = config_mod.load_relations()
    return frozenset(r["id"] for r in rels)


def insert_link(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    rel_type: str = "similar",
    confidence: float = 1.0,
    source: str = "manual",
) -> None:
    """建立页面关系（幂等：同三元组不重复）。

    T-14：写入前校验 rel_type 在 registry/relations.yaml 注册表内，
    未知类型抛 ValueError 拒绝（注册表权威，DB 无 CHECK 约束）。
    """
    known = _known_rel_types()
    if rel_type not in known:
        raise ValueError(
            f"未知关系类型 rel_type={rel_type!r}，"
            f"合法类型见 registry/relations.yaml（共 {len(known)} 种）"
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO wiki_links (source_id, target_id, rel_type, confidence, source, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (source_id, target_id, rel_type, confidence, source, _now_iso()),
    )
    conn.commit()


def list_links(conn: sqlite3.Connection, page_id: str) -> list[dict]:
    """某页的全部关系（出向 + 入向）。"""
    rows = conn.execute(
        "SELECT * FROM wiki_links WHERE source_id=? OR target_id=? ORDER BY confidence DESC",
        (page_id, page_id),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_links(conn: sqlite3.Connection, source_id: str) -> None:
    """删除某页全部出向关系。"""
    conn.execute("DELETE FROM wiki_links WHERE source_id=?", (source_id,))
    conn.commit()
