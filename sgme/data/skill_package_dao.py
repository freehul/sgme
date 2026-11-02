# -*- coding: utf-8 -*-
"""skill_package_dao.py：技能包附属文件与修订史数据层（2.0 设计 5.2：技能真源 git → 数据库）。

分工：``skills`` 表存 SKILL.md 本体与元数据；本模块存**其余附属文件**
（references/ / scripts/ / assets/ …）与**修订史**，与
``data/skill_package.py``（目录 ↔ 入库结构的双向通道）配套：
serialize 入库 → put_files 落库 → record_revision 留痕 →
list_revisions 回溯 → write_skill_package 反序列化落盘。

设计要点（仿 llm_usage_dao：自建表 + 幂等 + 纯 SQL）：
- 表 ``skill_files`` / ``skill_revisions`` 结构以本模块为准；``ensure_tables()``
  仍是幂等自建入口（独立连接/测试用），而 ``data/db.py`` 的 SKILLS_DDL 已
  正式并入同一份 CREATE（T-401，生产库启动即建，不再依赖调用方懒建）；
  两处结构由 tests/test_skills_db.py 对账钉死，改一处必须同步另一处
- skill_files 主键 (skill_name, rel_path)：put 重复即覆盖（replace）
- 修订史只增不改（append-only），替代 git 版本管理；created_at 用 UTC ISO
- package_sha256 口径与 skill_package.serialize_skill_dir 完全一致：
  按 path 字节序排序的 ``f"{sha256}  {path}\\n"`` 拼接串（UTF-8）取 sha256——
  这是与现有包通道对账的锚点，改公式必同步改两处
- 安全契约同 skill_package._validate_member_path：rel_path 视为不可信输入，
  拒绝 ``..`` / 绝对路径 / 反斜杠 / 盘符冒号 / 空段，**先全量校验后落库**

分层：本模块只做 SQL 与校验，不 import fastapi/mcp（data 层不认识协议）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from sgme.data.skill_package import _validate_member_path


def _rows_to_dicts(cur: sqlite3.Cursor) -> list[dict]:
    """游标行 → dict 列表（按 cursor.description 组键，不依赖连接级 row_factory）。"""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（修订史排序锚点）。"""
    return datetime.now(UTC).isoformat()


def ensure_tables(conn: sqlite3.Connection) -> None:
    """建 skill_files / skill_revisions 表与索引（CREATE IF NOT EXISTS，幂等）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_files (
          skill_name TEXT NOT NULL,
          rel_path   TEXT NOT NULL,
          content    BLOB NOT NULL,
          size       INTEGER NOT NULL,
          sha256     TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (skill_name, rel_path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_revisions (
          revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
          skill_name  TEXT NOT NULL,
          version     TEXT NOT NULL,
          package_sha256 TEXT NOT NULL,
          file_count  INTEGER NOT NULL,
          total_bytes INTEGER NOT NULL,
          note        TEXT,
          created_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_rev_name ON skill_revisions(skill_name, created_at)"
    )
    conn.commit()


def put_files(conn: sqlite3.Connection, skill_name: str, files: list[dict]) -> dict:
    """写入/覆盖一组附属文件，返回写入统计与聚合哈希。

    每个 file 形如 ``{"path": "references/api.md", "content": b"..."}``；
    同一 (skill_name, rel_path) 已存在则覆盖。**先全量校验（path + content
    类型）后落库**——任一成员非法即整批 ValueError，零部分写入。
    package_sha256 = 按 path 字节序排序的 ``f"{sha}  {path}\\n"`` 拼接串的
    sha256（与 skill_package.serialize_skill_dir 同口径，跨模块对账锚点）。
    """
    validated: list[tuple[str, bytes, str]] = []  # (rel_path, content, sha256)
    for f in files:
        path = f["path"]
        _validate_member_path(path)  # 复用包通道校验：../绝对路径/反斜杠/盘符/空段
        content = f["content"]
        if not isinstance(content, bytes):
            raise ValueError(f"非法文件内容（必须是 bytes）: {path!r}")
        validated.append((path, content, __import__("hashlib").sha256(content).hexdigest()))

    import hashlib

    rows = sorted(((p, sha) for p, _, sha in validated), key=lambda r: r[0].encode("utf-8"))
    concat = "".join(f"{sha}  {p}\n" for p, sha in rows)
    package_sha256 = hashlib.sha256(concat.encode("utf-8")).hexdigest()
    total_bytes = sum(len(c) for _, c, _ in validated)

    conn.executemany(
        """
        INSERT INTO skill_files (skill_name, rel_path, content, size, sha256, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(skill_name, rel_path) DO UPDATE SET
          content = excluded.content,
          size = excluded.size,
          sha256 = excluded.sha256,
          updated_at = excluded.updated_at
        """,
        [(skill_name, p, c, len(c), sha, _now_iso()) for p, c, sha in validated],
    )
    conn.commit()
    return {
        "written": len(validated),
        "total_bytes": total_bytes,
        "package_sha256": package_sha256,
    }


def list_files(conn: sqlite3.Connection, skill_name: str) -> list[dict]:
    """列出技能附属文件（按 path 排序；不含 content——列表页不拉大字段）。"""
    return _rows_to_dicts(conn.execute(
        """
        SELECT rel_path AS path, size, sha256, updated_at
        FROM skill_files
        WHERE skill_name = ?
        ORDER BY rel_path
        """,
        (skill_name,),
    ))


def get_file(conn: sqlite3.Connection, skill_name: str, rel_path: str) -> bytes | None:
    """取单个文件原字节；不存在返回 None。"""
    _validate_member_path(rel_path)
    row = conn.execute(
        "SELECT content FROM skill_files WHERE skill_name = ? AND rel_path = ?",
        (skill_name, rel_path),
    ).fetchone()
    return row[0] if row is not None else None


def delete_files(conn: sqlite3.Connection, skill_name: str, paths: list[str]) -> int:
    """删除指定附属文件，返回实际删除行数（先全量校验后执行）。"""
    for p in paths:
        _validate_member_path(p)
    with conn:  # 事务包裹：cursor.rowcount 累加即为删除行数
        deleted = 0
        for p in paths:
            cur = conn.execute(
                "DELETE FROM skill_files WHERE skill_name = ? AND rel_path = ?",
                (skill_name, p),
            )
            deleted += cur.rowcount
    return deleted


def record_revision(
    conn: sqlite3.Connection,
    skill_name: str,
    version: str,
    package_sha256: str,
    file_count: int,
    total_bytes: int,
    note: str | None = None,
) -> int:
    """追加一条修订记录（只增不改），返回 revision_id。"""
    cur = conn.execute(
        """
        INSERT INTO skill_revisions
          (skill_name, version, package_sha256, file_count, total_bytes, note, created_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (skill_name, version, package_sha256, int(file_count), int(total_bytes), note, _now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_revisions(conn: sqlite3.Connection, skill_name: str, limit: int = 20) -> list[dict]:
    """修订史（created_at DESC，最近 limit 条）。"""
    cur = conn.execute(
        """
        SELECT revision_id, skill_name, version, package_sha256,
               file_count, total_bytes, note, created_at
        FROM skill_revisions
        WHERE skill_name = ?
        ORDER BY created_at DESC, revision_id DESC
        LIMIT ?
        """,
        (skill_name, max(1, int(limit))),
    )
    return _rows_to_dicts(cur)


def latest_revision(conn: sqlite3.Connection, skill_name: str) -> dict | None:
    """最近一条修订；无历史返回 None。"""
    revs = list_revisions(conn, skill_name, limit=1)
    return revs[0] if revs else None
