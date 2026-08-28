"""operations/graph.py：知识图谱组装（ST-13）。

数据源（架构 §23 数据模型）：
- 场景节点：memory.db 的 scenes（active）
- 记忆节点：memory.db 的 memories（active）+ memory_tags 维度
- wiki 页面节点：wiki.db 的 wiki_pages（active）
- 边：
  - scene → memory：scene_memories（场景-记忆双向溯源）
  - wiki → wiki：wiki_links（页面关联，rel_type 见 registry/relations.yaml）

组装规则：
- 只取 active 场景/记忆/wiki 页面（rejected/expired 不进入图谱）
- 记忆节点仅包含「至少关联一个场景」的记忆（孤记忆不进图谱，避免图被无关节点撑爆）
- wiki 边仅保留两端都在节点集合内的（孤儿边丢弃，防幽灵链接）
- 规模控制：scene_limit（场景上限，含其关联记忆）/ wiki_limit（wiki 页面上限）

分层铁律：本模块不认识协议，SQL 全部落 sgme.data.* DAO。
"""
from __future__ import annotations

import sqlite3

from sgme.data import scene_dao
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
        mem_conn: memory.db 连接（scenes / memories / scene_memories）。
        wiki_conn: wiki.db 连接（wiki_pages / wiki_links），None 时跳过 wiki 层。
        scene_limit: 场景节点上限（默认 200）。
        wiki_limit: wiki 页面节点上限（默认 200）。
        memory_limit: 记忆节点上限（默认 3000，防超大库撑爆前端）。

    Returns:
        OperationResult(ok=True)，data 为：
        {
          "nodes": [{"id", "type": "scene"|"memory"|"wiki", "label",
                     "title", "heat", "memories_count", "dimensions", ...}],
          "links": [{"source", "target", "type": "scene_memory"|"wiki_link",
                     "rel_type"}],
          "stats": {"scenes": n, "memories": n, "wiki": n,
                    "scene_links": n, "wiki_links": n},
        }
    """
    nodes: list[dict] = []
    links: list[dict] = []
    node_ids: set[str] = set()

    # ── ① 场景节点 + scene→memory 边 ──
    scenes = scene_dao.list_active_scenes(mem_conn, limit=scene_limit)
    # 场景关联记忆计数（批量子查询，避免逐场景 N+1）
    scene_counts: dict[str, int] = {}
    if scenes:
        sc_ids = [s["scene_id"] for s in scenes]
        ph = ",".join("?" * len(sc_ids))
        for row in mem_conn.execute(
            f"SELECT scene_id, COUNT(*) AS c FROM scene_memories WHERE scene_id IN ({ph}) GROUP BY scene_id",
            sc_ids,
        ).fetchall():
            scene_counts[row["scene_id"]] = row["c"]

    for sc in scenes:
        nodes.append({
            "id": sc["scene_id"],
            "type": "scene",
            "label": (sc["title"] or sc["scene_id"])[:80],
            "title": sc["title"],
            "heat": sc.get("heat", 0),
            "status": sc.get("status", "active"),
            "memories_count": scene_counts.get(sc["scene_id"], 0),
            "created_at": sc.get("created_at"),
            "updated_at": sc.get("updated_at"),
        })
        node_ids.add(sc["scene_id"])
        for mid in scene_dao.list_memories_for_scene(mem_conn, sc["scene_id"]):
            links.append({
                "source": sc["scene_id"],
                "target": mid,
                "type": "scene_memory",
                "rel_type": "contains",
            })

    # ── ② 记忆节点（仅取场景关联的记忆，批量查维度） ──
    scene_mids: list[str] = []
    for l in links:
        if l["type"] == "scene_memory" and l["target"] not in scene_mids:
            scene_mids.append(l["target"])
    scene_mids = scene_mids[:memory_limit]

    if scene_mids:
        dim_map: dict[str, list[str]] = {}
        ph = ",".join("?" * len(scene_mids))
        for row in mem_conn.execute(
            f"SELECT memory_id, dimension_id FROM memory_tags WHERE memory_id IN ({ph})",
            scene_mids,
        ).fetchall():
            dim_map.setdefault(row["memory_id"], []).append(row["dimension_id"])

        ph2 = ",".join("?" * len(scene_mids))
        mem_rows = mem_conn.execute(
            f"""
            SELECT memory_id, content, memory_type, priority, status,
                   created_at, updated_at
            FROM memories
            WHERE memory_id IN ({ph2}) AND status='active'
            """,
            scene_mids,
        ).fetchall()
        for r in mem_rows:
            nodes.append({
                "id": r["memory_id"],
                "type": "memory",
                "label": (r["content"] or r["memory_id"])[:80],
                "content": r["content"],
                "memory_type": r["memory_type"],
                "priority": r["priority"],
                "status": r["status"],
                "dimensions": dim_map.get(r["memory_id"], []),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
            node_ids.add(r["memory_id"])

    # ── ②b 孤儿边丢弃：scene→memory 边的目标记忆必须已建节点（存在且 active），
    #     否则 d3.forceLink 会因找不到节点抛 "node not found"（记忆被删未清关联 /
    #     超 memory_limit 截断都会产生悬空边）。与下方 wiki 边孤儿过滤保持一致。
    links = [l for l in links if l["target"] in node_ids]

    # ── ③ wiki 页面节点 + wiki→wiki 边 ──
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
            ph3 = ",".join("?" * len(page_ids))
            # source 或 target 命中任一节点都算关联；绑定参数需与 IN 占位符一一对应
            link_rows = wiki_conn.execute(
                f"""
                SELECT source_id, target_id, rel_type
                FROM wiki_links
                WHERE source_id IN ({ph3}) OR target_id IN ({ph3})
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

    # ── ④ 统计 ──
    scene_count = sum(1 for n in nodes if n["type"] == "scene")
    mem_count = sum(1 for n in nodes if n["type"] == "memory")
    wiki_count = sum(1 for n in nodes if n["type"] == "wiki")
    scene_link_count = sum(1 for l in links if l["type"] == "scene_memory")

    return OperationResult.succeed({
        "nodes": nodes,
        "links": links,
        "stats": {
            "scenes": scene_count,
            "memories": mem_count,
            "wiki": wiki_count,
            "scene_links": scene_link_count,
            "wiki_links": wiki_links_count,
        },
    })
