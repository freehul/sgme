# -*- coding: utf-8 -*-
"""data/edge_dao.py：记忆关系边 memory_edges 读写 + 零 token 结构边 backfill（ST-38 T-133）。

data 层唯一 DB 访问（对称 demand_dao / idea_dao）。edge_id 确定性生成
``{from_id}::{to_id}::{relation}`` —— 幂等可重跑（INSERT OR IGNORE / 收敛式重插）。

关系集（≤6，进化方案 v0.2 §T2-1）：similar / causes / supersedes / belongs_to /
contradicts / evolves_from。source：'llm' | 'cooccur' | 'scene' | 'system'
（system = 结构性边，T2-1a backfill 产物；llm/cooccur 留给 T2-1b/T2-3）。

backfill（``backfill_system_edges``，纯 SQL 零 token）：
- 源① ``memory_archive.superseded_by`` 归档链 → 双向边
  （superseder→archived 记 ``supersedes``，archived→superseder 记 ``evolves_from``）；
- 场景共现源已随场景层退役摘除（T-427，2026-11-20）：历史 ``belongs_to`` 边
  **保留不删**，本函数只收敛重插 supersession 两类边。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any, Iterable

SYSTEM_SOURCE = "system"

#: backfill 收敛重插的关系白名单（场景共现 belongs_to 已停止重算，历史边保留）
_REBUILT_RELATIONS = ("supersedes", "evolves_from")

_INSERT_SQL = """INSERT OR IGNORE INTO memory_edges
   (edge_id, from_id, to_id, relation, weight, valid_from, valid_to, created_at, source)
   VALUES (?,?,?,?,?,?,?,?,?)"""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _edge_id(from_id: str, to_id: str, relation: str) -> str:
    return f"{from_id}::{to_id}::{relation}"


def create_edge(
    conn: sqlite3.Connection,
    from_id: str,
    to_id: str,
    relation: str,
    weight: float = 1.0,
    source: str = SYSTEM_SOURCE,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> str:
    """写入一条边（edge_id 确定性；INSERT OR IGNORE 幂等）。返回 edge_id。"""
    eid = _edge_id(from_id, to_id, relation)
    conn.execute(
        _INSERT_SQL,
        (eid, from_id, to_id, relation, weight, valid_from, valid_to, _now_iso(), source),
    )
    return eid


def delete_edges_by_source(conn: sqlite3.Connection, source: str) -> int:
    """删除指定 source 的全部边（backfill 收敛式重跑用）。返回删除行数。"""
    cur = conn.execute("DELETE FROM memory_edges WHERE source=?", (source,))
    return cur.rowcount


def delete_edges_for_memory(conn: sqlite3.Connection, memory_id: str) -> int:
    """删除与一条记忆相关的**全部**边（from_id 或 to_id 命中，双向）。返回删除行数。

    W03/W08（W01 §4 生命周期）：记忆归档/删除时清边，防止 update/merge/supersession
    之后图召回指向已不在 memories 表的 id。与 purge（memory_dao.delete_memory）同口径；
    memory_dao.archive_memory 在归档同一事务内做等价的 DELETE（data 层不跨模块回环）。

    幂等：无匹配返回 0；不自行提交（调用方决定事务边界）。
    """
    if not memory_id:
        return 0
    cur = conn.execute(
        "DELETE FROM memory_edges WHERE from_id=? OR to_id=?", (memory_id, memory_id))
    return cur.rowcount


#: `list_edges_for_memory` 的返回列（与 memory_edges 列同名，weight 原值不缩放）
_EDGE_FIELDS = ("edge_id", "from_id", "to_id", "relation", "weight", "source",
                "valid_from", "valid_to")


def _source_filter(raw: str | Iterable[str] | None) -> list[str]:
    """把 source 过滤参数（None / 单值 / 可迭代）规整为字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    return [str(s) for s in raw if s]


def list_edges_for_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    relations: Iterable[str] | None = None,
    source: str | Iterable[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """一条记忆的关联边（**双向**：from_id 或 to_id 命中），按 (relation, weight DESC) 排序。

    W06（W01 契约 §5）：`GET /v1/memory/{id}` / MCP `memory_get` 的 context.edges
    与 UI 「事件上下文」面板的读侧入口。

    Args:
        memory_id: 记忆 id；空串 → []。
        relations: 关系白名单（如 ("belongs_to", "causes")）；None/空 = 全部关系。
        source: source 白名单（单值或可迭代，如 "l2_extract"）；None = 全部来源。
        limit: 返回条数上限（None = 不限）。

    Returns:
        [{"edge_id","from_id","to_id","relation","weight","source","valid_from","valid_to"}]
        ——与 memory_edges 列同名；weight 为**原值**（不做关系级缩放，缩放是召回策略的事）。
    """
    if not memory_id:
        return []
    sql = (f"SELECT {', '.join(_EDGE_FIELDS)} FROM memory_edges "
           "WHERE (from_id=? OR to_id=?)")
    params: list[Any] = [memory_id, memory_id]
    rels = [str(r) for r in (relations or []) if r]
    if rels:
        sql += " AND relation IN (%s)" % ",".join("?" * len(rels))
        params.extend(rels)
    srcs = _source_filter(source)
    if srcs:
        sql += " AND source IN (%s)" % ",".join("?" * len(srcs))
        params.extend(srcs)
    sql += " ORDER BY relation, weight DESC, from_id, to_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


def count_edges(conn: sqlite3.Connection, source: str | None = None) -> int:
    """边总数；source 非空时只统计该 source。"""
    if source is None:
        cur = conn.execute("SELECT COUNT(*) AS c FROM memory_edges")
    else:
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_edges WHERE source=?", (source,))
    return cur.fetchone()["c"]


def list_edges(
    conn: sqlite3.Connection,
    source: str | None = None,
    relation: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """列出边（调试/对账用）。"""
    sql = ("SELECT edge_id, from_id, to_id, relation, weight, valid_from, valid_to, "
           "created_at, source FROM memory_edges")
    cond, params = [], []
    if source:
        cond.append("source=?")
        params.append(source)
    if relation:
        cond.append("relation=?")
        params.append(relation)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY created_at LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def neighbors(
    conn: sqlite3.Connection,
    memory_id: str,
    relation: str | None = None,
    limit: int | None = None,
    exclude_relations: Iterable[str] | None = None,
    relation_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """1-hop 邻居（双向 from/to；T-134 图召回预留）。

    同一邻居出现多关系时取 weight 最高的那条（按 memory_id 去重）。
    返回按 (weight DESC, memory_id) 排序的 [{"memory_id", "relation", "weight"}, ...]。

    v2（T-137）：`exclude_relations` 排除指定关系（如 contradicts 否定边不应参与
    联想召回）；`relation_weights` 对边权重做关系级缩放（归一化不同边类型尺度：
    语义边 LLM 置信 0-1 vs 共现边场景数 1-N）；两者缺省 → 与 v1 行为逐字节等价。
    """
    out: dict[str, dict[str, Any]] = {}
    rel_conds: list[str] = []
    excl = list(exclude_relations) if exclude_relations else []
    if excl:
        rel_conds.append("relation NOT IN (%s)" % ",".join("?" * len(excl)))
    for other_col, self_col in (("to_id", "from_id"), ("from_id", "to_id")):
        sql = (f"SELECT {other_col} AS nid, relation, weight FROM memory_edges "
               f"WHERE {self_col}=?")
        params: list[Any] = [memory_id]
        if relation:
            sql += " AND relation=?"
            params.append(relation)
        if rel_conds:
            sql += " AND " + " AND ".join(rel_conds)
            params.extend(excl)
        for r in conn.execute(sql, params):
            nid = r["nid"]
            w = r["weight"]
            if relation_weights and r["relation"] in relation_weights:
                w = w * relation_weights[r["relation"]]
            if nid not in out or w > out[nid]["weight"]:
                out[nid] = {
                    "memory_id": nid,
                    "relation": r["relation"],
                    "weight": w,
                }
    lst = sorted(out.values(), key=lambda x: (-x["weight"], x["memory_id"]))
    if limit:
        lst = lst[:limit]
    return lst


def backfill_system_edges(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """零 token 结构边 backfill（T2-1a，source='system'）。

    T-427（2026-11-20）：场景层退役，场景共现源已摘除；本函数只重算归档链
    双向边（supersedes / evolves_from）。历史 ``belongs_to`` 边**保留不删**，
    因此收敛重插只清理 ``_REBUILT_RELATIONS`` 白名单内的关系。

    Args:
        conn: memory.db 连接。
        dry_run: True 时只统计不写库（不 DELETE、不 INSERT）。

    Returns:
        统计 dict：superseded_pairs / supersedes_edges / evolves_from_edges / total。
    """
    stats: dict[str, Any] = {
        "superseded_pairs": 0,
        "supersedes_edges": 0,
        "evolves_from_edges": 0,
        "total": 0,
    }
    now = _now_iso()
    edges: list[tuple] = []  # (edge_id, from_id, to_id, relation, weight, vf, vt, now, source)

    # ---------- 归档链（memory_archive.superseded_by 非空） ----------
    cur = conn.execute(
        "SELECT memory_id, superseded_by FROM memory_archive "
        "WHERE superseded_by IS NOT NULL AND superseded_by != ''"
    )
    for row in cur:
        archived_id, superseder = row["memory_id"], row["superseded_by"]
        stats["superseded_pairs"] += 1
        edges.append((_edge_id(superseder, archived_id, "supersedes"),
                      superseder, archived_id, "supersedes", 1.0, None, None, now, SYSTEM_SOURCE))
        stats["supersedes_edges"] += 1
        edges.append((_edge_id(archived_id, superseder, "evolves_from"),
                      archived_id, superseder, "evolves_from", 1.0, None, None, now, SYSTEM_SOURCE))
        stats["evolves_from_edges"] += 1

    stats["total"] = len(edges)

    # ---------- 落库（dry_run 跳过；单事务收敛式重插） ----------
    if not dry_run:
        try:
            conn.execute("BEGIN")
            # 只清理本函数负责重算的关系：场景共现 belongs_to 属历史存量，保留不删
            placeholders = ",".join("?" * len(_REBUILT_RELATIONS))
            conn.execute(
                f"DELETE FROM memory_edges WHERE source=? AND relation IN ({placeholders})",
                (SYSTEM_SOURCE, *_REBUILT_RELATIONS),
            )
            conn.executemany(_INSERT_SQL, edges)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        stats["dry_run"] = True

    return stats


def edge_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """对账：按 source/relation 统计边量（运维/测试断言用）。"""
    by_source = {
        r["source"]: r["c"]
        for r in conn.execute(
            "SELECT source, COUNT(*) AS c FROM memory_edges GROUP BY source")
    }
    by_relation = {
        r["relation"]: r["c"]
        for r in conn.execute(
            "SELECT relation, COUNT(*) AS c FROM memory_edges GROUP BY relation")
    }
    return {
        "total": count_edges(conn),
        "by_source": by_source,
        "by_relation": by_relation,
    }
