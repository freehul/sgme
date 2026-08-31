# -*- coding: utf-8 -*-
"""data/edge_dao.py：记忆关系边 memory_edges 读写 + 零 token 结构边 backfill（ST-38 T-133）。

data 层唯一 DB 访问（对称 demand_dao / idea_dao / scene_dao）。edge_id 确定性生成
``{from_id}::{to_id}::{relation}`` —— 幂等可重跑（INSERT OR IGNORE / 收敛式重插）。

关系集（≤6，进化方案 v0.2 §T2-1）：similar / causes / supersedes / belongs_to /
contradicts / evolves_from。source：'llm' | 'cooccur' | 'scene' | 'system'
（system = 结构性边，T2-1a backfill 产物；llm/cooccur 留给 T2-1b/T2-3）。

backfill（``backfill_system_edges``，纯 SQL 零 token）：
- 源① ``memory_archive.superseded_by`` 归档链 → 双向边
  （superseder→archived 记 ``supersedes``，archived→superseder 记 ``evolves_from``）；
- 源② active 场景 ∩ active 记忆的同场景共现 → ``belongs_to`` 边
  （weight = 共现场景数，仅存规范方向 from_id < to_id；每场景先按 priority 取
  ``per_scene_top_n`` 截断防组合爆炸，再每记忆按 weight 取 ``top_n`` 邻，
  最后全局 ``global_cap`` 上限，超限记 anomaly_warn）。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any, Iterable

SYSTEM_SOURCE = "system"

# 结构边 backfill 默认参数（T2-1a，进化方案 v0.2 §T2-1「边量控制（必做）」）
DEFAULT_PER_SCENE_TOP_N = 100     # 每场景参与配对的记忆数上限（防 C(1239,2)=76.7 万爆炸）
DEFAULT_TOP_N = 8                 # 每记忆保留最多邻居数（按共现 weight 降序）
DEFAULT_MIN_WEIGHT = 1            # 共现场景数下限（1 = 任意共现即建边）
DEFAULT_GLOBAL_CAP = 200_000      # 设计建议总边量上限 ≤20 万

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
    per_scene_top_n: int = DEFAULT_PER_SCENE_TOP_N,
    top_n: int = DEFAULT_TOP_N,
    min_weight: int = DEFAULT_MIN_WEIGHT,
    global_cap: int = DEFAULT_GLOBAL_CAP,
    dry_run: bool = False,
    publish_anomaly: bool = True,
) -> dict[str, Any]:
    """零 token 结构边 backfill（T2-1a，source='system'）。

    Args:
        conn: memory.db 连接。
        per_scene_top_n: 每场景参与配对的记忆数上限（防组合爆炸；超限按 priority
            DESC、updated_at DESC 取前 N）。
        top_n: 每记忆保留最多邻居数（按共现 weight 降序）。
        min_weight: 共现场景数下限。
        global_cap: 总边量上限（supersession + belongs_to 合计）；超限按 weight
            降序裁剪 belongs_to 边并记 anomaly_warn。
        dry_run: True 时只统计不写库（不 DELETE、不 INSERT、不发布 anomaly）。
        publish_anomaly: 全局超限时是否发布 anomaly_warn 事件（signal_events）。

    Returns:
        统计 dict：superseded_pairs / supersedes_edges / evolves_from_edges /
        scene_pairs_raw / belongs_to_edges / truncated / total / anomaly。
    """
    stats: dict[str, Any] = {
        "superseded_pairs": 0,
        "supersedes_edges": 0,
        "evolves_from_edges": 0,
        "scene_pairs_raw": 0,
        "belongs_to_edges": 0,
        "truncated": 0,
        "total": 0,
        "anomaly": None,
    }
    now = _now_iso()
    edges: list[tuple] = []  # (edge_id, from_id, to_id, relation, weight, vf, vt, now, source)

    # ---------- 源① 归档链（memory_archive.superseded_by 非空） ----------
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

    # ---------- 源② 场景共现（active 场景 ∩ active 记忆） ----------
    # 每场景按 priority/updated_at 取前 per_scene_top_n 记忆参与配对
    scene_mems: dict[str, list[str]] = {}
    cur = conn.execute(
        """SELECT sm.scene_id, sm.memory_id
           FROM scene_memories sm
           JOIN scenes s ON s.scene_id = sm.scene_id AND s.status='active'
           JOIN memories m ON m.memory_id = sm.memory_id AND m.status='active'
           ORDER BY m.priority DESC, m.updated_at DESC"""
    )
    for row in cur:
        scene_mems.setdefault(row["scene_id"], []).append(row["memory_id"])

    pair_w: dict[tuple[str, str], int] = {}
    for scene_id, mems in scene_mems.items():
        if len(mems) > per_scene_top_n:
            mems = mems[:per_scene_top_n]
            stats["scene_capped"] = stats.get("scene_capped", 0) + 1
        n = len(mems)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = mems[i], mems[j]
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)  # 规范方向
                pair_w[key] = pair_w.get(key, 0) + 1
                stats["scene_pairs_raw"] += 1

    # 每记忆聚合邻居并按 weight 取 top_n
    per_mem: dict[str, list[tuple[str, int]]] = {}  # mem -> [(neighbor, weight)]
    for (a, b), w in pair_w.items():
        if w < min_weight:
            continue
        per_mem.setdefault(a, []).append((b, w))
        per_mem.setdefault(b, []).append((a, w))
    for mem, nbrs in per_mem.items():
        nbrs.sort(key=lambda x: (-x[1], x[0]))
        per_mem[mem] = nbrs[:top_n]

    # 生成 belongs_to 边（规范方向去重：同一对只插一次）
    seen_pairs: set[tuple[str, str]] = set()
    for mem, nbrs in per_mem.items():
        for nbr, w in nbrs:
            a, b = (mem, nbr) if mem < nbr else (nbr, mem)
            if (a, b) in seen_pairs:
                continue
            seen_pairs.add((a, b))
            edges.append((_edge_id(a, b, "belongs_to"),
                          a, b, "belongs_to", float(w), None, None, now, SYSTEM_SOURCE))
            stats["belongs_to_edges"] += 1

    # ---------- 全局上限（按 weight 降序裁剪 belongs_to） ----------
    if len(edges) > global_cap:
        belongs = [e for e in edges if e[3] == "belongs_to"]
        other = [e for e in edges if e[3] != "belongs_to"]
        keep = max(global_cap - len(other), 0)
        belongs.sort(key=lambda e: (-e[4], e[1], e[2]))
        dropped = len(belongs) - keep
        belongs = belongs[:keep]
        edges = other + belongs
        stats["truncated"] = dropped
        stats["anomaly"] = f"edge_total {len(edges)} capped at {global_cap}; dropped {dropped}"

    stats["total"] = len(edges)

    # ---------- 落库（dry_run 跳过；单事务收敛式重插） ----------
    if not dry_run:
        try:
            conn.execute("BEGIN")
            delete_edges_by_source(conn, SYSTEM_SOURCE)
            conn.executemany(_INSERT_SQL, edges)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if stats["anomaly"] and publish_anomaly:
            try:
                from sgme.signal import engine as signal_engine
                signal_engine.publish(
                    "anomaly_warn", "edge_backfill",
                    {"message": stats["anomaly"], "total": len(edges), "cap": global_cap},
                    conn,
                )
                stats["anomaly_published"] = True
            except Exception as e:  # noqa: BLE001 —— 告警发布失败不影响 backfill 结果
                stats["anomaly_publish_error"] = str(e)[:200]
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
