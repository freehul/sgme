"""data/chunk_cache_dao.py：块级提炼结果缓存 DAO（D-80，2026-11-06）。

问题背景（2026-11-06 实测）：块级提炼 + 零容忍提交闸下，失败文件的同一批块会被
反复重发——近 2,000 条提炼样本里同块平均重发 3.7 次，最坏文件 14.4 小时重发 23 轮
（835 次调用 / 45.9M token）。本 DAO 提供「同 prompt + 同模型 → 复用结果」，
让重跑只付失败块的代价，已成功块零 token。

缓存 key 口径：``sha256(prompt 全文 + NUL + model_fp)``
- prompt 全文已包含块文本、维度表、沉淀清单、提示词版本 → 任一变化自然失效；
- model_fp 覆盖「模型链变化」→ 换模型不会复用旧结果（D-80 决策：缓存必须绑定模型，
  否则换模型后仍读旧结果，故障会被缓存掩盖）。

边界：本模块只做存储，不含业务判断；表由 db.py 的 MEMORY_DDL 建，
另提供 ``ensure_table`` 兜底老库/单测临时库。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("sgme.data.chunk_cache_dao")

#: 默认缓存保留天数（refine.chunk_cache_ttl_days 覆盖）
DEFAULT_TTL_DAYS = 30

#: 默认缓存行数上限（refine.chunk_cache_max_rows 覆盖；防慢增长撑库）
DEFAULT_MAX_ROWS = 50000

_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS refine_chunk_cache (
  cache_key TEXT PRIMARY KEY,
  prompt_version TEXT,
  model_fp TEXT NOT NULL,
  chunk_sha TEXT NOT NULL,
  memories_json TEXT NOT NULL,
  provider TEXT,
  total_tokens INTEGER,
  created_at TEXT NOT NULL,
  last_hit_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  reused_tokens INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_refine_chunk_cache_created ON refine_chunk_cache(created_at);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_table(conn: sqlite3.Connection) -> None:
    """兜底建表（幂等）：db.py 的 MEMORY_DDL 已建，这里覆盖临时库/老库场景。"""
    try:
        conn.executescript(_CACHE_DDL)
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001——建表失败由调用方降级为「不缓存」
        logger.warning("refine_chunk_cache 建表失败（降级为不缓存）: %s", exc)


def cache_key(prompt: str, model_fp: str) -> str:
    """缓存键 = sha256(prompt 全文 + NUL + 模型指纹)。"""
    h = hashlib.sha256()
    h.update((prompt or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((model_fp or "").encode("utf-8"))
    return h.hexdigest()


def get(conn: sqlite3.Connection, key: str) -> dict | None:
    """查缓存。命中返回 {memories, provider, total_tokens, prompt_version}，未命中 None。"""
    try:
        row = conn.execute(
            "SELECT memories_json, provider, total_tokens, prompt_version "
            "FROM refine_chunk_cache WHERE cache_key=?",
            (key,),
        ).fetchone()
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("块级缓存读取失败（按未命中处理）: %s", exc)
        return None
    if row is None:
        return None
    try:
        memories = json.loads(row[0] or "[]")
    except (TypeError, ValueError):
        logger.warning("块级缓存内容损坏（按未命中处理）: key=%s", key[:16])
        return None
    if not isinstance(memories, list):
        return None
    return {
        "memories": memories,
        "provider": row[1],
        "total_tokens": int(row[2] or 0),
        "prompt_version": row[3],
    }


def put(
    conn: sqlite3.Connection,
    *,
    key: str,
    prompt_version: str | None,
    model_fp: str,
    chunk_sha: str,
    memories: list[dict],
    provider: str | None,
    total_tokens: int | None,
) -> None:
    """写缓存（幂等 upsert）。写失败只告警——缓存是旁路，绝不打断提炼。"""
    try:
        conn.execute(
            """
            INSERT INTO refine_chunk_cache
              (cache_key, prompt_version, model_fp, chunk_sha, memories_json,
               provider, total_tokens, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
              memories_json=excluded.memories_json,
              provider=excluded.provider,
              total_tokens=COALESCE(excluded.total_tokens, refine_chunk_cache.total_tokens),
              prompt_version=COALESCE(excluded.prompt_version, refine_chunk_cache.prompt_version)
            """,
            (
                key, prompt_version, model_fp, chunk_sha,
                json.dumps(memories or [], ensure_ascii=False),
                provider, total_tokens, _now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("块级缓存写入失败（不阻塞提炼）: %s", exc)


def record_hit(conn: sqlite3.Connection, key: str, reused_tokens: int) -> None:
    """累计命中次数与省下的 token（观测用；失败只告警）。"""
    try:
        conn.execute(
            "UPDATE refine_chunk_cache SET hit_count=hit_count+1, "
            "reused_tokens=reused_tokens+?, last_hit_at=? WHERE cache_key=?",
            (max(0, int(reused_tokens or 0)), _now_iso(), key),
        )
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("块级缓存命中计数失败（不阻塞）: %s", exc)


def purge(
    conn: sqlite3.Connection,
    ttl_days: int = DEFAULT_TTL_DAYS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> int:
    """清过期 + 超量缓存，返回删除行数（Dream 周期调用）。"""
    removed = 0
    try:
        if ttl_days and ttl_days > 0:
            cur = conn.execute(
                "DELETE FROM refine_chunk_cache "
                "WHERE julianday(created_at) < julianday('now', ?)",
                (f"-{int(ttl_days)} days",),
            )
            removed += max(0, cur.rowcount or 0)
        if max_rows and max_rows > 0:
            total = conn.execute("SELECT COUNT(*) FROM refine_chunk_cache").fetchone()[0]
            if total and total > max_rows:
                cur = conn.execute(
                    "DELETE FROM refine_chunk_cache WHERE cache_key IN ("
                    " SELECT cache_key FROM refine_chunk_cache"
                    " ORDER BY created_at ASC LIMIT ?)",
                    (int(total - max_rows),),
                )
                removed += max(0, cur.rowcount or 0)
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("块级缓存清理失败（下一轮再试）: %s", exc)
        return removed
    return removed


def stats(conn: sqlite3.Connection, since_iso: str) -> dict:
    """缓存规模与命中率（运维看板用）。表缺失/瞬间无数据 → 全 0。"""
    out = {"rows": 0, "hits": 0, "reused_tokens": 0, "hit_rate": 0.0}
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hit_count),0), COALESCE(SUM(reused_tokens),0) "
            "FROM refine_chunk_cache WHERE created_at >= ?",
            (since_iso,),
        ).fetchone()
        if row:
            out["rows"] = int(row[0] or 0)
            out["hits"] = int(row[1] or 0)
            out["reused_tokens"] = int(row[2] or 0)
            total = out["rows"] + out["hits"]
            out["hit_rate"] = round(out["hits"] / total, 4) if total else 0.0
    except sqlite3.Error:
        pass
    return out
