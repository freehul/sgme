"""raw 正文 FTS5（T-207 ①）：L0 会话原文全文检索与命中片段。

背景：2026-08-11 裁定「L0 仅作溯源根、正文不纳入检索」→ **T-207 推翻该裁定**
（v0.4 决策：expired 记忆出池减肥的前提是 L0 可回溯——否则技术细节既不在
记忆池、又搜不到 L0 = 知识丢失）。此前 sessions scope 只做元数据 LIKE
（file_id/session_key/agent_id/path），「能取回（按 ID 读全文）、不能找到
（搜不到正文）」。

设计：
- 虚表形态：**contentful FTS5**（正文在磁盘 ``raw/<subdir>/<file_id>.md``，
  不在 DB 表内，无法用 external content）。索引列 ``body_seg``（segment()
  分词，与 memories_fts / scenes_fts 同口径）+ ``file_id`` UNINDEXED。
- 增量维护：``raw_fts_state(file_id, body_hash)`` 判重——正文只增不改
  （增量段追加），hash 变了才重建该行，重跑幂等。
- 降级：FTS5 不可用 / 表为空 → 检索退回元数据 LIKE（调用方 session_dao
  既有行为），本模块所有写操作 best-effort 不炸主流程。
- 启动零开销：``init_raw_fts`` 只建表（毫秒级）；全量索引由后台线程 /
  admin API 显式触发（``rebuild_raw_fts``），append 链路只做单文件增量。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sgme.data.search import stoplist as stoplist_mod
from sgme.data.search import _build_fts_query
from sgme.segment import segment

logger = logging.getLogger("sgme.data.search.raw_fts")

RAW_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(
    body_seg,
    file_id UNINDEXED
);
"""

RAW_FTS_STATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_fts_state (
  file_id TEXT PRIMARY KEY,
  body_hash TEXT NOT NULL,
  indexed_at TEXT NOT NULL
);
"""


def init_raw_fts(session_conn: sqlite3.Connection) -> None:
    """建 raw_fts 虚表与状态表（幂等，毫秒级，启动挂载零负担）。

    失败仅 WARNING（FTS5 模块缺失等），search 侧退回元数据 LIKE。
    """
    try:
        session_conn.executescript(RAW_FTS_DDL)
        session_conn.executescript(RAW_FTS_STATE_DDL)
        session_conn.commit()
    except Exception as e:  # FTS5 不可用等 → 检索侧降级，不炸启动
        logger.warning("raw_fts 初始化失败，sessions 正文检索不可用: %s", e)


def _raw_md_files(raw_dir: Path) -> list[Path]:
    """遍历 raw 目录全部 L0 md 文件（含子目录）。"""
    root = Path(raw_dir)
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def _body_text(path: Path) -> str | None:
    """读 L0 文件并剥掉 frontmatter（正文 = 消息块；frontmatter 不参与检索）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("raw 文件读取失败（跳过）: %s: %s", path, e)
        return None
    if text.startswith("---"):
        lines = text.splitlines(keepends=True)
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "".join(lines[i + 1:])
        return None  # frontmatter 未闭合 = 格式损坏，跳过
    return text


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def index_raw_file(session_conn: sqlite3.Connection, path: Path) -> bool:
    """单文件增量索引（hash 判重，幂等）。返回是否实际（重）建索引。

    append 链路调用（写盘后 best-effort）；正文读取/解析失败返回 False 不抛。
    """
    body = _body_text(path)
    if body is None or not body.strip():
        return False
    file_id = path.stem
    h = _body_hash(body)
    row = session_conn.execute(
        "SELECT body_hash FROM raw_fts_state WHERE file_id=?", (file_id,)
    ).fetchone()
    if row and row["body_hash"] == h:
        return False
    try:
        session_conn.execute("BEGIN")
        session_conn.execute("DELETE FROM raw_fts WHERE file_id=?", (file_id,))
        session_conn.execute(
            "INSERT INTO raw_fts(body_seg, file_id) VALUES(?, ?)",
            (segment(body), file_id),
        )
        session_conn.execute(
            "INSERT INTO raw_fts_state(file_id, body_hash, indexed_at) VALUES(?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET body_hash=excluded.body_hash, "
            "indexed_at=excluded.indexed_at",
            (file_id, h, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        session_conn.commit()
        return True
    except Exception as e:
        session_conn.rollback()
        logger.warning("raw_fts 单文件索引失败 file=%s: %s", file_id, e)
        return False


def rebuild_raw_fts(
    session_conn: sqlite3.Connection,
    raw_dir: Path,
    incremental: bool = True,
) -> dict:
    """全量/增量重建 raw 正文索引。返回统计 {total, indexed, skipped, failed}。

    - incremental=True：body_hash 未变的文件跳过（重跑幂等，秒级）
    - incremental=False：清空重建（分词口径漂移时用）
    耗时与文件数线性（1225 文件 × 分词），调用方决定前台/后台执行。
    """
    init_raw_fts(session_conn)
    if not incremental:
        session_conn.execute("DELETE FROM raw_fts")
        session_conn.execute("DELETE FROM raw_fts_state")
        session_conn.commit()
    stats = {"total": 0, "indexed": 0, "skipped": 0, "failed": 0}
    for path in _raw_md_files(raw_dir):
        stats["total"] += 1
        try:
            if index_raw_file(session_conn, path):
                stats["indexed"] += 1
            else:
                stats["skipped"] += 1
        except Exception:
            stats["failed"] += 1
    logger.info(
        "raw_fts 重建完成: total=%(total)s indexed=%(indexed)s skipped=%(skipped)s failed=%(failed)s",
        stats,
    )
    return stats


def raw_fts_coverage(session_conn: sqlite3.Connection) -> dict:
    """索引覆盖率（health/巡检用）：磁盘文件数 vs 已索引数。"""
    try:
        indexed = session_conn.execute("SELECT COUNT(*) FROM raw_fts_state").fetchone()[0]
        return {"indexed": indexed}
    except sqlite3.OperationalError:
        return {"indexed": 0, "error": "raw_fts 未初始化"}


def search_raw_body(
    session_conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    use_stoplist: bool = True,
) -> list[dict]:
    """正文 FTS 检索：返回 [{file_id, score}]（score = bm25，越小越相关）。

    - 查询侧与 memories_fts 同口径（segment + 停用词 + OR 连接）
    - FTS5 不可用 / 空表 → []（调用方退回元数据 LIKE，行为不变）
    """
    stripped = (query or "").strip()
    if not stripped:
        return []
    try:
        fts_query = _build_fts_query(stripped, use_stoplist=use_stoplist)
        cur = session_conn.execute(
            """
            SELECT f.file_id, bm25(raw_fts) AS score
            FROM raw_fts f
            WHERE raw_fts MATCH ?
            ORDER BY score ASC
            LIMIT ?
            """,
            (fts_query, int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError as e:
        logger.warning("raw_fts 检索不可用（退回元数据 LIKE）: %s", e)
        return []


def extract_hit_snippet(body: str, query: str, window: int = 100) -> str | None:
    """在原文中定位 query 首个词并截取前后窗口（命中片段，替代固定前 200 字符）。

    - 词表：query 按 segment_terms 分词，取长度 ≥2 的前 4 个词依次尝试
      （与检索词同源；全部未命中 → 返回 None，调用方回退文件开头）
    - 窗口：命中位置前后各 window 字符，边界对齐到行首优先
    """
    if not body:
        return None
    from sgme.segment import segment_terms

    terms = [t for t in segment_terms(query) if len(t) >= 2][:4]
    if not terms:
        terms = [query.strip()]
    lower = body.lower()
    pos = -1
    hit_term = None
    for t in terms:
        pos = lower.find(t.lower())
        if pos >= 0:
            hit_term = t
            break
    if pos < 0:
        return None
    start = max(0, pos - window)
    end = min(len(body), pos + len(hit_term or "") + window)
    # 边界对齐行首（避免从半行中间开始，可读性差）
    line_start = body.rfind("\n", start, pos)
    if line_start > start:
        start = line_start + 1
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return f"{prefix}{body[start:end].strip()}{suffix}"
