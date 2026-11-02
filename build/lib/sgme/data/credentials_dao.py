"""sgme/data/credentials_dao.py：凭据库（credentials.db）数据层（T-268，EP-11）。

独立库判据（方案 §三）：敏感面隔离 / 爆炸半径控制——凭据密文不进记忆库的
提炼/检索/评测/运维脚本等操作面，可单独权限收紧、单独恢复。

本模块 = 该库唯一数据操作层：
- ``connect_credentials``：连接 + 幂等建表 + schema_versions 登记（与
  sgme.data.db 三库同款口径：WAL / 外键 / row_factory；凭据库独立版本线，
  不进三库迁移链——备份/恢复场景只做文件级复制，连库即建表仅发生在
  服务启动与 restore 重开两处）
- CRUD / 软删 / 恢复（**对已删键名写入的 409 语义由 operations 层裁决**，
  本层不自动复活）
- 审计记账。⚠️ 写函数**不自行 commit**：写/删审计与业务写同事务、
  读类 best-effort，提交/回滚一律由 operations 层编排。

行形状纪律（sgme-development 三.21）：查询结果一律经 ``_rows_to_dicts`` 由
cursor.description 构造，兼容 sqlite3.Row 与裸连接普通元组两种形态。

未来 schema 变更：照 data/db.py ``_migrate_*`` 模式在本模块追加幂等迁移
函数并在 ``connect_credentials`` 内调用。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sgme import config

# 独立库版本线（label credentials_v1）：新库无历史包袱，不共用三库的全局版本号
SCHEMA_VERSION = 1

# ---------- DDL（方案 §三：credentials + credential_audit + (key_name, at) 索引） ----------

CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS credentials (
  id           INTEGER PRIMARY KEY,
  key_name     TEXT UNIQUE NOT NULL,
  value_cipher TEXT NOT NULL,
  description  TEXT,
  status       TEXT NOT NULL DEFAULT 'active',
  deleted_at   TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  updated_by   TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credential_audit (
  id       INTEGER PRIMARY KEY,
  key_name TEXT NOT NULL,
  action   TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  value_fp TEXT,
  at       TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cred_audit_key_at ON credential_audit(key_name, at);
"""

_SCHEMA_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_versions (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL);
"""


def now_iso() -> str:
    """UTC ISO 8601 时间戳（与 data/db.py 同款口径）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: Path) -> sqlite3.Connection:
    """打开连接：WAL + 外键 + row_factory（与 data/db.py::_connect 同款口径）。

    凭据库自持一份实现（不跨模块引 db.py 私有函数）：credentials.db 的连接
    唯一入口就是本模块，且不进三库迁移链。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def connect_credentials(data_dir: str | Path | None = None) -> sqlite3.Connection:
    """打开 credentials.db 连接并确保 schema 就绪（幂等建表 + 版本登记）。"""
    d = Path(data_dir) if data_dir else config.DATA_DIR
    conn = _connect(d / "credentials.db")
    conn.executescript(CREDENTIALS_DDL)
    conn.executescript(_SCHEMA_VERSIONS_DDL)
    cur = conn.execute(
        "SELECT version FROM schema_versions WHERE version=?", (SCHEMA_VERSION,)
    )
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO schema_versions (version, name, applied_at) VALUES (?,?,?)",
            (SCHEMA_VERSION, "credentials_v1", now_iso()),
        )
    conn.commit()
    return conn


def _rows_to_dicts(cur: sqlite3.Cursor) -> list[dict]:
    """查询结果 → dict 列表（由 cursor.description 取列名，兼容裸连接元组行）。"""
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------- 读 ----------


def list_credentials(conn: sqlite3.Connection, *, include_deleted: bool = False) -> list[dict]:
    """列表（键名 + 元数据，**不含值**；key_name 升序）。默认过滤已删（方案 §三）。"""
    sql = (
        "SELECT key_name, description, status, created_at, updated_at, updated_by, deleted_at "
        "FROM credentials"
    )
    if not include_deleted:
        sql += " WHERE status = 'active'"
    sql += " ORDER BY key_name ASC"
    return _rows_to_dicts(conn.execute(sql))


def get_credential(
    conn: sqlite3.Connection, key_name: str, *, include_deleted: bool = False
) -> dict | None:
    """单条完整行（含 value_cipher）；软删行默认不可见。"""
    sql = (
        "SELECT key_name, value_cipher, description, status, created_at, updated_at, "
        "updated_by, deleted_at FROM credentials WHERE key_name = ?"
    )
    if not include_deleted:
        sql += " AND status = 'active'"
    rows = _rows_to_dicts(conn.execute(sql, (key_name,)))
    return rows[0] if rows else None


def get_first_cipher(conn: sqlite3.Connection) -> str | None:
    """库内首条密文（主密钥解密探测用；含已删行——历史密文同样锚定主密钥）。"""
    row = conn.execute(
        "SELECT value_cipher FROM credentials ORDER BY id ASC LIMIT 1"
    ).fetchone()
    return row[0] if row is not None else None


# ---------- 写（不自动 commit：审计同事务由 operations 层编排） ----------


def insert_credential(
    conn: sqlite3.Connection,
    *,
    key_name: str,
    value_cipher: str,
    description: str | None,
    actor: str,
    at: str,
) -> None:
    """插入新凭据（created_at = updated_at = at）。"""
    conn.execute(
        "INSERT INTO credentials (key_name, value_cipher, description, status, deleted_at, "
        "created_at, updated_at, updated_by) VALUES (?,?,?,'active',NULL,?,?,?)",
        (key_name, value_cipher, description, at, at, actor),
    )


def update_credential(
    conn: sqlite3.Connection,
    *,
    key_name: str,
    value_cipher: str,
    description: str | None,
    actor: str,
    at: str,
) -> bool:
    """更新值（仅 active 行；description '更新即覆盖'，None 即清空）。返回是否命中行。"""
    cur = conn.execute(
        "UPDATE credentials SET value_cipher=?, description=?, updated_at=?, updated_by=? "
        "WHERE key_name=? AND status='active'",
        (value_cipher, description, at, actor, key_name),
    )
    return cur.rowcount > 0


def soft_delete_credential(conn: sqlite3.Connection, key_name: str, *, at: str) -> bool:
    """软删（幂等：已删/不存在均 no-op）。返回本次是否发生状态变化。"""
    cur = conn.execute(
        "UPDATE credentials SET status='deleted', deleted_at=? "
        "WHERE key_name=? AND status='active'",
        (at, key_name),
    )
    return cur.rowcount > 0


def restore_credential(conn: sqlite3.Connection, key_name: str) -> bool:
    """恢复：status='active' 且清空 deleted_at（幂等：active 行 no-op）。返回是否变化。"""
    cur = conn.execute(
        "UPDATE credentials SET status='active', deleted_at=NULL "
        "WHERE key_name=? AND status='deleted'",
        (key_name,),
    )
    return cur.rowcount > 0


def value_fingerprint(value: str) -> str:
    """值指纹 sha256[:8]（判真实变更、不泄值；单向摘要不可逆推明文）。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def record_audit(
    conn: sqlite3.Connection,
    *,
    key_name: str,
    action: str,
    agent_id: str,
    value_fp: str | None = None,
    at: str,
) -> None:
    """审计记账（read/export/write/delete/restore）。提交策略由调用方编排。"""
    conn.execute(
        "INSERT INTO credential_audit (key_name, action, agent_id, value_fp, at) "
        "VALUES (?,?,?,?,?)",
        (key_name, action, agent_id, value_fp, at),
    )
