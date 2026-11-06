"""data/chunk_failure_dao.py：块级失败补跑队列 DAO（D-80，2026-11-06）。

语义（与 2026-11-03 的「块失败不得静默丢内容」修复对齐）：
- 有记忆产出的块**照常落库**、游标照常推进；
- 失败的块单独入队（带最终块文本 + 沉淀清单），由 engine/chunk_replay.py 逐块补跑；
- 补跑成功即出队；累计失败达 refine.chunk_replay_max_attempts → 保留记录但不再重试
  （运维可见缺口，不再是「整份文件无限重发」）。

表由 db.py 的 MEMORY_DDL 建，另提供 ensure_table 兜底。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("sgme.data.chunk_failure_dao")

#: 默认单块补跑上限（refine.chunk_replay_max_attempts 覆盖）
DEFAULT_MAX_ATTEMPTS = 3

_FAILURE_DDL = """
CREATE TABLE IF NOT EXISTS refine_chunk_failures (
  failure_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL DEFAULT 0,
  chunk_sha TEXT NOT NULL,
  prompt_version TEXT,
  chunk_text TEXT NOT NULL,
  sunk_refs_text TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_at TEXT NOT NULL,
  UNIQUE(file_id, chunk_sha));
CREATE INDEX IF NOT EXISTS idx_refine_chunk_failures_file
  ON refine_chunk_failures(file_id, last_at);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_table(conn: sqlite3.Connection) -> None:
    """兜底建表（幂等）。失败只告警——入队失败时调用方回退到「整份阻断」老路径。"""
    try:
        conn.executescript(_FAILURE_DDL)
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("refine_chunk_failures 建表失败: %s", exc)


def enqueue(
    conn: sqlite3.Connection,
    *,
    file_id: str,
    chunk_index: int,
    chunk_sha: str,
    prompt_version: str | None,
    chunk_text: str,
    sunk_refs_text: str | None,
    error: str | None,
) -> bool:
    """登记一个失败块（同 file_id + chunk_sha 幂等：只累加 attempts 并刷新原因）。

    Returns:
        True = 已登记；False = 写库失败（调用方应回退到整份阻断，宁可重跑不丢内容）。
    """
    now = _now_iso()
    try:
        conn.execute(
            """
            INSERT INTO refine_chunk_failures
              (failure_id, file_id, chunk_index, chunk_sha, prompt_version,
               chunk_text, sunk_refs_text, error, attempts, created_at, last_at)
            VALUES (?,?,?,?,?,?,?,?,1,?,?)
            ON CONFLICT(file_id, chunk_sha) DO UPDATE SET
              attempts = refine_chunk_failures.attempts + 1,
              error = excluded.error,
              chunk_index = excluded.chunk_index,
              last_at = excluded.last_at
            """,
            (
                str(uuid.uuid4()), file_id, int(chunk_index), chunk_sha,
                prompt_version, chunk_text, sunk_refs_text, error, now, now,
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("失败块入队失败（file=%s）: %s", file_id, exc)
        return False


def list_pending(
    conn: sqlite3.Connection,
    limit: int = 20,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[dict]:
    """取待补跑块（attempts < max_attempts，按入队时间升序）。"""
    try:
        rows = conn.execute(
            """
            SELECT failure_id, file_id, chunk_index, chunk_sha, prompt_version,
                   chunk_text, sunk_refs_text, error, attempts
            FROM refine_chunk_failures
            WHERE attempts < ?
            ORDER BY last_at ASC LIMIT ?
            """,
            (max(1, int(max_attempts)), max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("失败块队列读取失败: %s", exc)
        return []
    keys = ("failure_id", "file_id", "chunk_index", "chunk_sha",
            "prompt_version", "chunk_text", "sunk_refs_text", "error", "attempts")
    return [dict(zip(keys, row)) for row in rows]


def resolve(conn: sqlite3.Connection, failure_id: str) -> bool:
    """补跑成功 → 出队。"""
    try:
        cur = conn.execute(
            "DELETE FROM refine_chunk_failures WHERE failure_id=?", (failure_id,)
        )
        conn.commit()
        return (cur.rowcount or 0) > 0
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("失败块出队失败（failure_id=%s）: %s", failure_id, exc)
        return False


def bump(conn: sqlite3.Connection, failure_id: str, error: str | None) -> int:
    """补跑再次失败 → attempts+1 + 刷新原因，返回累计次数（0 = 记录不存在）。"""
    try:
        conn.execute(
            "UPDATE refine_chunk_failures SET attempts=attempts+1, error=?, last_at=? "
            "WHERE failure_id=?",
            ((error or "")[:500] or None, _now_iso(), failure_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT attempts FROM refine_chunk_failures WHERE failure_id=?", (failure_id,)
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("失败块重试计数失败（failure_id=%s）: %s", failure_id, exc)
        return 0


def count(conn: sqlite3.Connection) -> int:
    """当前积压块数（运维看板用）。"""
    try:
        row = conn.execute("SELECT COUNT(*) FROM refine_chunk_failures").fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0
