"""storage/near_dup_dao.py：L1.5 F1 近似查重隔离表 DAO（near_dup_quarantine）。

读写隔离表的唯一入口（纯 storage 层，conn 由调用方传入；风格照 refine_dao.py）。
链路：engine._near_dup_check 命中近似重复 → ``upsert_hit`` 暂存内容（不落 memories）；
人工复核（scripts/review_quarantine.py）后 → ``mark_discarded`` 丢弃 / ``restore`` 旁路补录。

- ``upsert_hit``：写命中（去重键 UNIQUE(matched_memory_id, content_hash)，同事实
  反复触发只更新分数不增行）；
- ``list_items`` / ``get_item``：复核浏览（dimension_ids JSON 容错解析）；
- ``mark_discarded``：pending → discarded；
- ``restore``：旁路补录——复用 memory_dao.insert_memory 全字段写入 + 向量即时回填
  （v4 §一 补录路径），写回 status='restored' + restored_memory_id + restore_reason。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from sgme.data import memory_dao
from sgme.data.search import vector as vector_mod

logger = logging.getLogger("sgme.data.near_dup_dao")


def _hash_content(content: str) -> str:
    """内容去重键：sha256(content)[:16]（与隔离表 DDL 注释口径一致）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _decode_item(row_dict: dict) -> dict:
    """行 dict 后处理：dimension_ids JSON 容错解析（异常 / None / 非列表 → []）。"""
    raw = row_dict.get("dimension_ids")
    dims: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            dims = [str(x) for x in parsed]
    row_dict["dimension_ids"] = dims
    return row_dict


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    """单行 dict 化（连接未开 row_factory 时按列名兜底——本模块可能被裸连接调用）。"""
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetch_all(conn: sqlite3.Connection, sql: str, params: list) -> list[dict]:
    """多行 dict 化（裸连接兜底，同上）。"""
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], sqlite3.Row):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


# ---------- 写 ----------

def upsert_hit(
    conn: sqlite3.Connection,
    content: str,
    matched_memory_id: str,
    score: float,
    source_ref: str | None = None,
    dimension_ids: list[str] | None = None,
) -> None:
    """命中写入（幂等去重）：同 (matched_memory_id, content_hash) 只更分不增行。

    - ``content_hash`` = sha256(content)[:16]；
    - ``ON CONFLICT ... DO UPDATE SET score=MAX(score, excluded.score)``（v4 §一
      写入语义：同事实反复触发时分数只升不降，复核成本不放大）；
    - ``dimension_ids`` 非 None 时 json.dumps(ensure_ascii=False) 落库；读取侧容错
      解析（见 ``get_item`` / ``list_items``）。
    """
    conn.execute(
        """
        INSERT INTO near_dup_quarantine
          (content, content_hash, matched_memory_id, score, source_ref, dimension_ids)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(matched_memory_id, content_hash) DO UPDATE SET
          score=MAX(score, excluded.score)
        """,
        (
            content,
            _hash_content(content),
            matched_memory_id,
            float(score),
            source_ref,
            json.dumps(dimension_ids, ensure_ascii=False) if dimension_ids is not None else None,
        ),
    )
    conn.commit()


def mark_discarded(conn: sqlite3.Connection, id: int) -> bool:
    """标记丢弃：仅 pending → discarded；非 pending（已丢弃/已补录）返回 False。"""
    cur = conn.execute(
        "UPDATE near_dup_quarantine SET status='discarded' WHERE id=? AND status='pending'",
        (id,),
    )
    conn.commit()
    return cur.rowcount > 0


def restore(
    conn: sqlite3.Connection,
    id: int,
    reason: str,
    cfg: dict,
    client=None,
) -> str:
    """旁路补录：隔离行内容写入 memories（全字段）+ 向量即时回填 + 回写状态。返回新 memory_id。

    v4 §一 补录路径：
    1. content / source_ref / dimension_ids 取自隔离行；memory_type='persona'、
       priority=50、time_velocity='static'（L1.5 同款保守默认）；
    2. TTL 经 ``ttl_policy.backfill_ttl`` 按维度默认回填（延迟导入：避免 storage
       模块加载期跨层 import engine；ttl_policy 为全项目单一 TTL 实现、与
       l15._backfill_ttl 同源、防两份漂移；storage 层直写豁免与 backup 裸连接同款、
       不走 engine 裁决链）；
    3. 复用 ``memory_dao.insert_memory`` 全字段写入（content_seg / memory_tags /
       memory_sources 一并覆盖，杜绝「直接 INSERT 漏标签/分词」）；
    4. ``vector_mod.upsert_memory_vector`` 即时回填（单条成本可忽略）；失败不阻塞
       ——backfill_vectors.py 无参重跑按「active 且无向量」LEFT JOIN 扫描天然捡起；
    5. 回写 status='restored' + restored_memory_id + restore_reason。
       补录后内容已 active，_near_dup_check 不会再拦（跳过 L1.5），杜绝补录死循环。

    拒绝场景（ValueError）：id 不存在；该行已是 restored（已补录一次）。
    """
    item = get_item(conn, id)
    if item is None:
        raise ValueError(f"隔离行不存在: id={id}")
    if item["status"] == "restored":
        raise ValueError(f"隔离行已补录: id={id} → restored_memory_id={item.get('restored_memory_id')}")

    dims = item["dimension_ids"] or []
    # 延迟导入（见 docstring：防加载期跨层循环；与 l15._backfill_ttl 同源）
    from sgme.engine.ttl_policy import backfill_ttl

    ttl = backfill_ttl(None, dims, (cfg or {}).get("dimensions") or [], memory_type="persona")

    source_ref = item.get("source_ref")
    sources = [(source_ref, "session")] if source_ref else None
    new_id = memory_dao.insert_memory(
        conn,
        content=item["content"],
        memory_type="persona",
        priority=50,
        time_velocity="static",
        ttl_days=ttl,
        dimension_ids=dims,
        sources=sources,
    )

    # 向量即时回填（失败不阻塞——日志提示无参重跑 backfill_vectors.py 兜底）
    ok = False
    try:
        ok = bool(vector_mod.upsert_memory_vector(conn, new_id, item["content"], cfg, client=client))
    except Exception as e:  # embed 客户端异常同样不阻塞补录
        logger.warning("near_dup restore: 向量即时回填异常（不阻塞补录）: %s", e)
    if not ok:
        logger.warning(
            "near_dup restore: 向量即时回填失败（不阻塞补录）"
            "——可无参重跑 scripts/backfill_vectors.py 兜底"
        )

    conn.execute(
        """
        UPDATE near_dup_quarantine
        SET status='restored', restored_memory_id=?, restore_reason=?
        WHERE id=?
        """,
        (new_id, reason, id),
    )
    conn.commit()
    return new_id


# ---------- 读 ----------

def get_item(conn: sqlite3.Connection, id: int) -> dict | None:
    """按 id 取隔离行（dimension_ids 解析为 list，缺/异常 → []）；不存在返回 None。"""
    d = _fetch_one(conn, "SELECT * FROM near_dup_quarantine WHERE id=?", (id,))
    return _decode_item(d) if d else None


def list_items(
    conn: sqlite3.Connection,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """复核浏览：按 id DESC 列出隔离行（可选 status 过滤；dimension_ids 容错解析）。"""
    sql = "SELECT * FROM near_dup_quarantine"
    params: list = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [int(limit), int(offset)]
    return [_decode_item(d) for d in _fetch_all(conn, sql, params)]
