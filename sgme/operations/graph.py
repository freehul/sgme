"""operations/graph.py：知识图谱组装（ST-13）。

数据源（架构 §23 数据模型）：
- wiki 页面节点：wiki.db 的 wiki_pages（active）
- 边：wiki → wiki（wiki_links，页面关联，rel_type 见 registry/relations.yaml）

组装规则：
- 只取 active wiki 页面
- wiki 边仅保留两端都在节点集合内的（孤儿边丢弃，防幽灵链接）
- 规模控制：wiki_limit（wiki 页面上限）

T-427（2026-11-20）：场景层正式退役——不再产出 scene 节点与 scene_memory 边；
`scene_limit` / `memory_limit` 形参只为 HTTP 契约兼容保留（无作用）。

分层铁律：本模块不认识协议，SQL 全部落 sgme.data.* DAO 或 wiki 扩展连接。
"""
from __future__ import annotations

import sqlite3

from sgme.operations.errors import OperationResult


def get_graph(
    mem_conn: sqlite3.Connection,
    wiki_conn: sqlite3.Connection | None = None,
    *,
    scene_limit: int = 200,
    wiki_limit: int = 200,
    memory_limit: int = 3000,
) -> OperationResult:
    """组装知识图谱：{nodes: [...], links: [...]}。

    Args:
        mem_conn: memory.db 连接（场景层退役后本函数不再读它；保留形参兼容调用方）。
        wiki_conn: wiki.db 连接（wiki_pages / wiki_links），None 时返回空图。
        scene_limit: 历史形参（场景层退役，无作用）。
        wiki_limit: wiki 页面节点上限（默认 200）。
        memory_limit: 历史形参（场景层退役，无作用）。

    Returns:
        OperationResult(ok=True)，data 为：
        {
          "nodes": [{"id", "type": "wiki", "label", "title", "category", ...}],
          "links": [{"source", "target", "type": "wiki_link", "rel_type"}],
          "stats": {"wiki": n, "wiki_links": n},
        }
    """
    nodes: list[dict] = []
    links: list[dict] = []
    node_ids: set[str] = set()
    wiki_links_count = 0

    if wiki_conn is not None:
        page_rows = wiki_conn.execute(
            """
            SELECT page_id, title, category, tags, status, updated_at
            FROM wiki_pages
            WHERE status='active' OR status IS NULL
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (wiki_limit,),
        ).fetchall()
        page_ids = [r["page_id"] for r in page_rows]
        for r in page_rows:
            nodes.append({
                "id": r["page_id"],
                "type": "wiki",
                "label": (r["title"] or r["page_id"])[:80],
                "title": r["title"],
                "category": r["category"],
                "status": r["status"] or "active",
                "updated_at": r["updated_at"],
            })
            node_ids.add(r["page_id"])

        if page_ids:
            ph = ",".join("?" * len(page_ids))
            # source 或 target 命中任一节点都算关联；绑定参数需与 IN 占位符一一对应
            link_rows = wiki_conn.execute(
                f"""
                SELECT source_id, target_id, rel_type
                FROM wiki_links
                WHERE source_id IN ({ph}) OR target_id IN ({ph})
                """,
                page_ids + page_ids,
            ).fetchall()
            for lr in link_rows:
                # 孤儿边丢弃：两端必须都在节点集合
                if lr["source_id"] not in node_ids or lr["target_id"] not in node_ids:
                    continue
                links.append({
                    "source": lr["source_id"],
                    "target": lr["target_id"],
                    "type": "wiki_link",
                    "rel_type": lr["rel_type"],
                })
                wiki_links_count += 1

    wiki_count = sum(1 for n in nodes if n["type"] == "wiki")

    return OperationResult.succeed({
        "nodes": nodes,
        "links": links,
        "stats": {
            "wiki": wiki_count,
            "wiki_links": wiki_links_count,
        },
    })
