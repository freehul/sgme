"""storage/session_dao.py：session.db 的 DAO（raw_files 会话原文索引）。

v0.7 三库拆分：本模块的 8 个函数由 `storage/wiki_dao.py` 原样迁入，
**函数名、签名、函数体一行不改**，只改所在文件与传入连接（wiki_conn → session_conn）。

raw_files 是原始层索引：file_id → path/session/agent/时间/提炼游标。

⚠️ 命名陷阱（勿踩）：本模块的 `update_refine_cursor()` 操作的是
`raw_files.last_refined_seq` 字段（按文件记录已提炼到的消息序号），
与 v0.7 新增的 `refine_cursor(namespace, date_label)` 表**毫无关系**。
新表的 DAO 是阶段 3 P3-T5 才新建的 `storage/refine_cursor_dao.py`，二者并存互补（D6）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def insert_raw_file(
    conn: sqlite3.Connection,
    file_id: str,
    path: str,
    session_key: str,
    started_at: str | None = None,
    agent_id: str | None = None,
    agent_model: str | None = None,
    ended_at: str | None = None,
    refined_at: str | None = None,
    last_refined_seq: int | None = None,
    status: str = "new",
    size: int | None = None,
    content_hash: str | None = None,
) -> str:
    """插入或更新 raw_files 行（按 file_id 幂等）。

    agent_model（T-43）：session 声明的提炼模型（provider/model），
    提炼动态链据此跟随 agent 当前 LLM。
    """
    conn.execute(
        """
        INSERT INTO raw_files
          (file_id, path, session_key, agent_id, agent_model, started_at, ended_at,
           refined_at, last_refined_seq, status, size, content_hash)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(file_id) DO UPDATE SET
          path=excluded.path,
          session_key=excluded.session_key,
          agent_id=excluded.agent_id,
          agent_model=excluded.agent_model,
          started_at=excluded.started_at,
          ended_at=excluded.ended_at,
          status=excluded.status,
          size=excluded.size,
          content_hash=excluded.content_hash
        """,
        (file_id, path, session_key, agent_id, agent_model, started_at, ended_at,
         refined_at, last_refined_seq, status, size, content_hash),
    )
    conn.commit()
    return file_id


def get_raw_file(conn: sqlite3.Connection, file_id: str) -> dict | None:
    cur = conn.execute("SELECT * FROM raw_files WHERE file_id=?", (file_id,))
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    # 普通 tuple：按列名映射（连接未开 row_factory 时兜底）
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_raw_file_by_session(conn: sqlite3.Connection, session_key: str) -> dict | None:
    """按 session_key 查询（最小闭环：一会话一文件）。"""
    cur = conn.execute(
        "SELECT * FROM raw_files WHERE session_key=? ORDER BY started_at DESC LIMIT 1",
        (session_key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def update_refine_cursor(
    conn: sqlite3.Connection,
    file_id: str,
    last_refined_seq: int,
    refined_at: str | None = None,
    status: str = "refined",
) -> bool:
    """更新提炼游标：last_refined_seq + refined_at + status。

    ⚠️ 操作对象是 `raw_files` 表，**不是** v0.7 新增的 `refine_cursor` 表（见模块 docstring）。
    """
    r_at = refined_at or _now_iso()
    cur = conn.execute(
        """
        UPDATE raw_files
        SET last_refined_seq=?, refined_at=?, status=?
        WHERE file_id=?
        """,
        (last_refined_seq, r_at, status, file_id),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_status(conn: sqlite3.Connection, file_id: str, status: str, ended_at: str | None = None, size: int | None = None) -> bool:
    """更新文件状态（追加新段时重置为 new，错误时设 error）。"""
    sets = ["status=?"]
    params: list = [status]
    if ended_at:
        sets.append("ended_at=?")
        params.append(ended_at)
    if size is not None:
        sets.append("size=?")
        params.append(size)
    params.append(file_id)
    cur = conn.execute(
        f"UPDATE raw_files SET {', '.join(sets)} WHERE file_id=?",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


def update_content_hash(conn: sqlite3.Connection, file_id: str, content_hash: str) -> bool:
    """更新 raw 文件内容哈希（append 追加后内容变化时调用）。"""
    cur = conn.execute(
        "UPDATE raw_files SET content_hash=? WHERE file_id=?",
        (content_hash, file_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_by_status(conn: sqlite3.Connection, status: str = "new", limit: int = 100) -> list[dict]:
    """按状态列出文件（提炼 batch 扫描用）。"""
    cur = conn.execute(
        "SELECT * FROM raw_files WHERE status=? ORDER BY COALESCE(refined_at, started_at) ASC LIMIT ?",
        (status, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def count_by_status(conn: sqlite3.Connection, status: str = "new") -> int:
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM raw_files WHERE status=?", (status,)
    )
    return cur.fetchone()["c"]

# ---------- 浏览分页（0.8 T-15 / 契约 §5.6.1） ----------

#: 契约 §5.6.1 的响应字段（顺序即响应键序）。``path`` / ``content_hash`` /
#: ``last_refined_seq`` 是内部实现细节，不外泄——尤其 path 会暴露服务端目录结构。
_BROWSE_RAW_COLUMNS: tuple[str, ...] = (
    "file_id", "session_key", "agent_id", "status", "size",
    "started_at", "ended_at", "refined_at",
)


def _like_contains(value: str) -> str:
    """把子串构造成 LIKE 模式，并转义 LIKE 元字符。

    不转义的话，用户传入的 ``%`` / ``_`` 会被当通配符，``hermes_1`` 会意外
    匹配 ``hermes-1``。配合 SQL 里的 ``ESCAPE '\\'`` 使用。
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_raw_files_page(
    conn: sqlite3.Connection,
    *,
    page: int = 1,
    limit: int = 50,
    session_key: str | None = None,
    agent_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[list[dict], int]:
    """L0 会话（raw_files）分页查询（契约 §5.6.1）。

    与 ``list_by_status`` 的分工：那个是提炼 batch 扫描用（单状态、按提炼水位
    升序、无 total）；本函数面向 UI 浏览——多条件过滤、按 started_at 倒序、带总数。

    Args:
        conn: session.db 连接（v0.7 拆分后 raw_files 在 session.db，勿查 wiki.db）。
        page: 页码（≥ 1，调用方已校验）。
        limit: 页大小（1..200，调用方已校验）。
        session_key: **子串**匹配（契约 §5.6.1，如 ``hermes-`` 前缀过滤）。
        agent_id: 精确匹配。
        status: ``new`` / ``refined`` / ``archived``；None 不过滤。
        since / until: 作用于 ``started_at`` 的闭区间边界。

    Returns:
        ``(items, total)``。
    """
    where: list[str] = []
    params: list = []
    if session_key:
        where.append("session_key LIKE ? ESCAPE '\\'")
        params.append(_like_contains(session_key))
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    if status:
        where.append("status = ?")
        params.append(status)
    if since:
        where.append("started_at >= ?")
        params.append(since)
    if until:
        where.append("started_at <= ?")
        params.append(until)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM raw_files{where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT {', '.join(_BROWSE_RAW_COLUMNS)} FROM raw_files{where_sql}
        ORDER BY started_at DESC, file_id DESC
        LIMIT ? OFFSET ?
        """,
        params + [int(limit), max(0, (int(page) - 1) * int(limit))],
    ).fetchall()

    return [dict(r) for r in rows], int(total)


# ---------- L0 检索（ST-33：/v1/search 新增 sessions scope） ----------

def search_raw_files(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """L0 会话原文索引检索（ST-33 scope="sessions"）：LIKE 子串匹配元数据列。

    raw_files 是原始层**索引表**——正文在磁盘 ``raw/<subdir>/<file_id>.md``
    （表内无正文列），故匹配目标是可检索的元数据列：file_id / session_key /
    agent_id / path（任一命中即召回，OR 语义）。正文摘要由 operations 层
    按需读盘（best-effort，见 ``operations/search.py::_search_sessions``）。

    命中行按最近会话排序（``COALESCE(ended_at, started_at) DESC``），
    与 ``list_raw_files_page`` 浏览分页同口径；空 query → 空列表（v0.6
    空串检索返回空结果的统一语义）。

    Args:
        conn: session.db 连接（v0.7 拆分后 raw_files 在 session.db，勿查 wiki.db）。
        query: 检索词（子串匹配；``%`` / ``_`` 按字面处理，防通配符注入）。
        limit: 返回条数上限。

    Returns:
        list[dict]：file_id / session_key / agent_id / started_at / ended_at /
        status / path（按最近会话倒序）。
    """
    stripped = (query or "").strip()
    if not stripped:
        return []
    like = _like_contains(stripped)
    rows = conn.execute(
        """
        SELECT file_id, session_key, agent_id, started_at, ended_at, status, path
        FROM raw_files
        WHERE file_id LIKE ? ESCAPE '\\'
           OR session_key LIKE ? ESCAPE '\\'
           OR agent_id LIKE ? ESCAPE '\\'
           OR path LIKE ? ESCAPE '\\'
        ORDER BY COALESCE(ended_at, started_at) DESC, file_id DESC
        LIMIT ?
        """,
        (like, like, like, like, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]
