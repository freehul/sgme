# -*- coding: utf-8 -*-
"""sgme/data/wiki_dao.py 补齐：wiki_pages / wiki_links CRUD（v0.7 §10 wiki 扩展模块）。

v0.7 三库拆分后本模块被清空（raw_files → session_dao，scenes 系列 → scene_dao）。
阶段 3（2026-08-08）补齐 wiki 扩展专用函数，对应 WIKI_DDL 收缩后的两张表。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sgme import wiki_classify


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


# ---------- 知识库 2.0（D-62 / §5.1）：分类注册表 / 修订史 ----------

def ensure_categories(conn: sqlite3.Connection) -> None:
    """确保 7 类默认分类注册（INSERT OR IGNORE 幂等）。"""
    now = _now_iso()
    for name, display, sort_order in wiki_classify.DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO wiki_categories "
            "(name, display_name, sort_order, is_default, created_at) VALUES (?,?,?,1,?)",
            (name, display, sort_order, now),
        )


def list_categories(conn: sqlite3.Connection) -> list[dict]:
    """分类注册表（按 sort_order 升序）。"""
    rows = conn.execute(
        "SELECT name, display_name, sort_order, is_default FROM wiki_categories "
        "ORDER BY sort_order, name"
    ).fetchall()
    return [dict(r) for r in rows]


def _content_hash(text: str | None) -> str:
    """内容指纹（sha256 前 16 位；去重/变更检测用）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _next_revision_no(conn: sqlite3.Connection, page_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(revision_no), 0) FROM wiki_revisions WHERE page_id=?", (page_id,)
    ).fetchone()
    return int(row[0]) + 1


def add_revision(
    conn: sqlite3.Connection,
    page_id: str,
    *,
    title: str | None,
    content: str,
    change_kind: str = "update",
    author: str | None = None,
    summary: str | None = None,
) -> int:
    """追加一条修订史（append-only），返回 revision_no。不提交事务（调用方统一 commit）。"""
    rev_no = _next_revision_no(conn, page_id)
    conn.execute(
        "INSERT INTO wiki_revisions "
        "(page_id, revision_no, title, content, change_kind, author, summary, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (page_id, rev_no, title, content, change_kind, author, summary, _now_iso()),
    )
    return rev_no


def unclassified_stats(conn: sqlite3.Connection, *, active_only: bool = True) -> dict:
    """未分类占比（>5% 应告警，D-62 / D-47 不许静默）。

    口径：category_class 为未分类或 NULL（未回填）都算未分类——回填后 NULL 应清零。
    """
    where = " WHERE status='active'" if active_only else ""
    total = int(conn.execute(f"SELECT COUNT(*) FROM wiki_pages{where}").fetchone()[0])
    if total == 0:
        return {"total": 0, "unclassified": 0, "ratio": 0.0}
    unc = int(
        conn.execute(
            f"SELECT COUNT(*) FROM wiki_pages{where}{' AND' if where else ' WHERE'} "
            "(category_class IS NULL OR category_class = ?)",
            (wiki_classify.UNCLASSIFIED,),
        ).fetchone()[0]
    )
    return {"total": total, "unclassified": unc, "ratio": round(unc / total, 4)}


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
    page_type: str | None = None,
    updated_by: str | None = None,
    write_revision: bool = True,
    revision_summary: str | None = None,
) -> str:
    """插入 wiki 页面（按 page_id 幂等：已存在则更新内容与元数据）。

    W1 新增：description（L1 摘要，描述即索引）/ author / status / supersedes
    （多 agent 共享溯源，方案 v0.3 §5.1）。description_seg 由 _seg 计算。
    status 缺省 'active'。

    知识库 2.0（D-62 / §5.1，2026-11-04）：
    - 分类：category 入参按 7 类注册表归一（wiki_classify.classify_category）；
      原始路径（如 research/dsh）保留为 tag origin_category:<raw>，不丢溯源；
    - 页型 page_type：由分类/标题推断（entity / topic / log / index）；
    - 标题行 headline：正文首行（≤30 字），空则回退 title；
    - 内容指纹 content_hash：sha256 前 16 位；
    - write_revision=True 时写一条 wiki_revisions（新建=create / 覆盖=update）；
    - updated_by：user / agent_<id> / system（缺省回退 author）。

    Returns:
        page_id。
    """
    now = ingested_at or _now_iso()
    ensure_categories(conn)
    cls = wiki_classify.classify_category(category, title, content)
    ptype = page_type or wiki_classify.page_type_for(cls, title)
    head = wiki_classify.headline(content, title)
    chash = _content_hash(content)
    updater = updated_by or author or "system"
    exists = (
        conn.execute("SELECT 1 FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone()
        is not None
    )
    conn.execute(
        """
        INSERT INTO wiki_pages (page_id, title, content, category, tags,
                                source_type, source_url, source_file,
                                ingested_at, updated_at, content_seg,
                                description, description_seg, author, status, supersedes,
                                page_type, category_class, headline, content_hash, updated_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            supersedes=excluded.supersedes,
            page_type=excluded.page_type,
            category_class=excluded.category_class,
            headline=excluded.headline,
            content_hash=excluded.content_hash,
            updated_by=excluded.updated_by
        """,
        (
            page_id, title, content, category,
            json.dumps(tags, ensure_ascii=False) if tags else None,
            source_type, source_url, source_file,
            now, now, _seg(content),
            description, _seg(description),
            author, status if status is not None else "active", supersedes,
            ptype, cls, head, chash, updater,
        ),
    )
    if write_revision:
        add_revision(
            conn, page_id, title=title, content=content,
            change_kind="update" if exists else "create",
            author=updater,
            summary=revision_summary or ("页面更新" if exists else "页面创建"),
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
    status: str = "active",
) -> list[dict]:
    """页面列表（按 updated_at 降序；category 可选过滤；status 过滤）。

    status: 'active'（默认）只返 active；'all' 返全部（含 superseded 历史）；
    其余值按 status 列等值过滤（如 'superseded'）。status 过滤口径与
    count_pages 一致，避免 total 与返回集分裂。
    """
    clauses: list[str] = []
    params: list = []
    if category:
        clauses.append("category=?")
        params.append(category)
    if status and status != "all":
        clauses.append("status=?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM wiki_pages{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [_parse_tags(dict(r)) for r in rows]


def update_page_content(
    conn: sqlite3.Connection,
    page_id: str,
    content: str,
    title: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
    *,
    change_kind: str = "update",
    author: str | None = None,
    updated_by: str | None = None,
    write_revision: bool = True,
    summary: str | None = None,
) -> bool:
    """更新页面内容与可选元数据（title/category/tags/description 传 None 表示不修改）。

    W1：description 显式传入时同步重算 description_seg（API 层 PATCH 默认不动
    description，本函数仅数据层能力）。

    知识库 2.0（2026-11-04）：
    - category 显式传入时按 7 类注册表归一（未传入保持原值，不静默改用户的分类）；
    - page_type / headline / content_hash 每次更新都重算；
    - write_revision=True 时追加修订史（change_kind：update / merge / rollback）。
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
    cls = wiki_classify.classify_category(new_cat, new_title, content)
    ptype = wiki_classify.page_type_for(cls, new_title)
    head = wiki_classify.headline(content, new_title)
    chash = _content_hash(content)
    updater = updated_by or author or "system"
    conn.execute(
        """
        UPDATE wiki_pages SET title=?, content=?, category=?, tags=?,
               updated_at=?, content_seg=?,
               description=COALESCE(?, description),
               description_seg=COALESCE(?, description_seg),
               page_type=?, category_class=?, headline=?, content_hash=?, updated_by=?
        WHERE page_id=?
        """,
        (new_title, content, new_cat, new_tags, _now_iso(), _seg(content),
         description, _seg(description) if description is not None else None,
         ptype, cls, head, chash, updater, page_id),
    )
    if write_revision:
        add_revision(
            conn, page_id, title=new_title, content=content,
            change_kind=change_kind, author=updater, summary=summary or "内容更新",
        )
    conn.commit()
    return True


def find_active_same_title(
    conn: sqlite3.Connection,
    title: str,
    category: str | None,
) -> dict | None:
    """查「同 title + 同 category + status='active'」的页（LIMIT 1）。

    category 匹配正确处理 None：category 为 None 时用 category IS NULL，
    否则 category=?（SQL 中 =? 无法命中 NULL 行）。命中返回 _parse_tags(dict(row))，
    否则 None。
    """
    if category is not None:
        row = conn.execute(
            "SELECT * FROM wiki_pages WHERE title=? AND category=? AND status='active' LIMIT 1",
            (title, category),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM wiki_pages WHERE title=? AND category IS NULL AND status='active' LIMIT 1",
            (title,),
        ).fetchone()
    if row is None:
        return None
    return _parse_tags(dict(row))


def mark_superseded(conn: sqlite3.Connection, page_id: str, supersedes_by: str) -> bool:
    """将旧页标记为已被取代：status='superseded' + supersedes=新 page_id。

    Returns:
        是否影响至少一行（rowcount > 0）。
    """
    cur = conn.execute(
        "UPDATE wiki_pages SET status='superseded', supersedes=? WHERE page_id=?",
        (supersedes_by, page_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_page(conn: sqlite3.Connection, page_id: str) -> bool:
    """删除页面（级联删除关联链接）。"""
    cur = conn.execute("SELECT 1 FROM wiki_pages WHERE page_id=?", (page_id,))
    if cur.fetchone() is None:
        return False
    conn.execute("DELETE FROM wiki_links WHERE source_id=? OR target_id=?", (page_id, page_id))
    conn.execute("DELETE FROM wiki_pages WHERE page_id=?", (page_id,))
    conn.commit()
    return True


def count_pages(conn: sqlite3.Connection, status: str | None = None) -> int:
    """页面总数（status=None 全表；否则按 status 等值计数，与 list_pages 口径一致）。"""
    if status and status != "all":
        return conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE status=?", (status,)
        ).fetchone()[0]
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
