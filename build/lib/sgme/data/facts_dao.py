"""sgme/data/facts_dao.py：T-136 原子事实三元组符号层查询（D4 JSON 列 MVP）。

`memories.facts_json` 存 JSON 数组：[{"subject": ..., "predicate": ..., "object": ...}]。
本模块用 SQLite JSON1 `json_each` 展开做符号层精确/子串查询——不依赖分词、
不依赖向量，是「XX 在哪家公司」这类事实性问句的确定性命中路径。

- query_facts：按 subject/predicate/object 任意组合过滤（exact 精确 = / 子串 LIKE）
- count_facts / edge_stats 式对账：facts_total / memories_with_facts
- list_facts_by_memory：单条记忆的三元组展开
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

_FACT_FIELDS = ("subject", "predicate", "object")


def _norm_fact(f: dict) -> dict | None:
    """规范化单条三元组（T-136 与 l1/_validate_item 同构校验）。"""
    if not isinstance(f, dict):
        return None
    out = {}
    for k in _FACT_FIELDS:
        v = str(f.get(k) or "").strip()
        if not v:
            return None
        out[k] = v
    return out


def parse_facts_json(raw: str | None) -> list[dict]:
    """facts_json 列值 → 三元组列表（容错，坏 JSON → []）。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for f in data:
        nf = _norm_fact(f)
        if nf:
            out.append(nf)
    return out


def _like(v: str) -> str:
    return f"%{v}%"


def query_facts(
    conn: sqlite3.Connection,
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    exact: bool = True,
    limit: int = 20,
    only_active: bool = True,
) -> list[dict]:
    """符号层事实查询：按三要素任意组合过滤（AND 语义）。

    - exact=True：精确等值匹配（=）；exact=False：子串匹配（LIKE）
    - only_active：只查 status='active'（默认）；False 时连归档一起查
    - 返回 [{memory_id, content, subject, predicate, object}]
    """
    conds: list[str] = []
    params: list[Any] = []
    for field, val in (("subject", subject), ("predicate", predicate), ("object", object)):
        if not val:
            continue
        col = f"json_extract(f.value, '$.{field}')"
        if exact:
            conds.append(f"{col} = ?")
            params.append(val)
        else:
            conds.append(f"{col} LIKE ?")
            params.append(_like(val))
    if only_active:
        conds.append("m.status = 'active'")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    sql = f"""
        SELECT m.memory_id, m.content,
               json_extract(f.value, '$.subject')  AS subject,
               json_extract(f.value, '$.predicate') AS predicate,
               json_extract(f.value, '$.object')   AS object
        FROM memories m, json_each(m.facts_json) AS f
        {where}
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_facts_by_memory(conn: sqlite3.Connection, memory_id: str) -> list[dict]:
    """单条记忆的三元组列表（含归档）。"""
    row = conn.execute(
        "SELECT facts_json FROM memories WHERE memory_id=?", (memory_id,)
    ).fetchone()
    if not row:
        return []
    return parse_facts_json(row["facts_json"])


def count_facts(conn: sqlite3.Connection) -> dict[str, int]:
    """对账：facts 总量 / 带 facts 的记忆数 / 三要素覆盖（运维 + 测试断言）。"""
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM memories m, json_each(m.facts_json) AS f"
    ).fetchone()["c"]
    mems = conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE facts_json IS NOT NULL AND facts_json != ''"
    ).fetchone()["c"]
    return {"facts_total": int(total), "memories_with_facts": int(mems)}


def get_memory_facts_json(conn: sqlite3.Connection, memory_id: str) -> str | None:
    """读原始 facts_json 列（测试/迁移对账用）。"""
    row = conn.execute(
        "SELECT facts_json FROM memories WHERE memory_id=?", (memory_id,)
    ).fetchone()
    return row["facts_json"] if row else None
