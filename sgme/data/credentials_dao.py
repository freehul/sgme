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
- 文件面（D-37 v1.2）：credential_files 表（Fernet 密文 BLOB + filename/size/sha256）；
  状态仍以 credentials 行（status）为唯一真源，本表不存状态（避免双写状态源）
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

# 独立库版本线：新库无历史包袱，不共用三库的全局版本号
# v2（2026-11-04 · SGME 2.0 迁移阶段 1）：补 scope / cred_type / expires_at /
# last_used_at 四列（D-37 扩展位；写读接线随 v1.1 落地，本阶段只保证结构存在）
# v3（2026-11-08 · D-37 v1.2）：新增 credential_files 表（文件/二进制「重要数据」，
# Fernet 密文 BLOB）。登记名跟随**结构代际**：credentials_v2 = 带文件表的第二代
# （v1 时代登记名是 credentials_v1 → 老库连库即补建表并追加 v2 行，幂等）。
SCHEMA_VERSION = 3
SCHEMA_NAME = "credentials_v2"

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
  updated_by   TEXT NOT NULL,
  scope        TEXT,      -- 访问范围（D-37 扩展位：agent 可读 / admin-only）
  cred_type    TEXT,      -- 凭据类型（D-37 扩展位：api_key / token / password / file）
  expires_at   TEXT,      -- 过期时间（D-37 扩展位；NULL = 不过期）
  last_used_at TEXT);     -- 最近使用时间（D-37 扩展位；读端触达时更新）
CREATE TABLE IF NOT EXISTS credential_audit (
  id       INTEGER PRIMARY KEY,
  key_name TEXT NOT NULL,
  action   TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  value_fp TEXT,
  at       TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cred_audit_key_at ON credential_audit(key_name, at);
CREATE TABLE IF NOT EXISTS credential_files (
  key_name    TEXT PRIMARY KEY,
  filename    TEXT NOT NULL,      -- 展示/下载用的文件名（已 basename 化，无路径无控制字符）
  size        INTEGER NOT NULL,   -- 明文字节数
  sha256      TEXT NOT NULL,      -- 明文 sha256（完整性校验 / 审计指纹来源）
  data_cipher BLOB NOT NULL,      -- Fernet token 字节（密文，绝不明文落库）
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  updated_by  TEXT NOT NULL);
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
    # 2.0 迁移阶段 1（2026-11-04）：老库补 D-37 四列（ALTER 幂等，1.x 读写零影响）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(credentials)").fetchall()]
    for col in ("scope", "cred_type", "expires_at", "last_used_at"):
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE credentials ADD COLUMN {col} TEXT")
    conn.executescript(_SCHEMA_VERSIONS_DDL)
    # 幂等：同版本号或同名（credentials_v2）已有登记行就不再插（老库/新建库同路径；
    # credential_files 由上方 CREDENTIALS_DDL 的 IF NOT EXISTS 在老库上就地补建）
    cur = conn.execute(
        "SELECT version FROM schema_versions WHERE version=? OR name=?",
        (SCHEMA_VERSION, SCHEMA_NAME),
    )
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO schema_versions (version, name, applied_at) VALUES (?,?,?)",
            (SCHEMA_VERSION, SCHEMA_NAME, now_iso()),
        )
    conn.commit()
    return conn


def _rows_to_dicts(cur: sqlite3.Cursor) -> list[dict]:
    """查询结果 → dict 列表（由 cursor.description 取列名，兼容裸连接元组行）。"""
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------- 读 ----------


def list_credentials(conn: sqlite3.Connection, *, include_deleted: bool = False) -> list[dict]:
    """列表（键名 + 元数据，**不含值**；key_name 升序）。默认过滤已删（方案 §三）。

    D-37 v1.1（2026-11-08）：返回列补 scope / cred_type / expires_at / last_used_at
    ——过滤判定在 operations 层（本层只做 SQL）。
    """
    sql = (
        "SELECT key_name, description, status, created_at, updated_at, updated_by, deleted_at, "
        "scope, cred_type, expires_at, last_used_at "
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
        "updated_by, deleted_at, scope, cred_type, expires_at, last_used_at "
        "FROM credentials WHERE key_name = ?"
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


# ---------- 文件表（D-37 v1.2；密文 BLOB，跟随 credentials 行状态） ----------

_FILE_META_COLS = "key_name, filename, size, sha256, created_at, updated_at, updated_by"


def get_file(conn: sqlite3.Connection, key_name: str) -> dict | None:
    """单条文件行（**含 data_cipher**）；不存在 → None。软删判定由 operations 层按
    credentials 行 status 裁决（本表不存状态，避免双写状态源）。"""
    rows = _rows_to_dicts(
        conn.execute(
            f"SELECT {_FILE_META_COLS}, data_cipher FROM credential_files WHERE key_name = ?",
            (key_name,),
        )
    )
    return rows[0] if rows else None


def upsert_file(
    conn: sqlite3.Connection,
    *,
    key_name: str,
    filename: str,
    size: int,
    sha256: str,
    data_cipher: bytes,
    actor: str,
    at: str,
) -> bool:
    """写入/替换文件行（created_at 保持首次写入值）。返回本次是否新建。

    不自动 commit（与审计同事务由 operations 层编排）。
    """
    existing = get_file(conn, key_name)
    if existing is None:
        conn.execute(
            "INSERT INTO credential_files "
            "(key_name, filename, size, sha256, data_cipher, created_at, updated_at, updated_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key_name, filename, int(size), sha256, sqlite3.Binary(data_cipher), at, at, actor),
        )
        return True
    conn.execute(
        "UPDATE credential_files SET filename=?, size=?, sha256=?, data_cipher=?, "
        "updated_at=?, updated_by=? WHERE key_name=?",
        (filename, int(size), sha256, sqlite3.Binary(data_cipher), at, actor, key_name),
    )
    return False


def list_file_meta(conn: sqlite3.Connection) -> dict[str, dict]:
    """全部文件行的**元数据**（不含密文）→ {key_name: {filename, size, sha256, ...}}。

    供 operations 层给列表视图补 filename / file_size / file_sha256（一次查询，免 N+1）。
    """
    rows = _rows_to_dicts(conn.execute(f"SELECT {_FILE_META_COLS} FROM credential_files"))
    return {row["key_name"]: row for row in rows}


# ---------- 写（不自动 commit：审计同事务由 operations 层编排） ----------


def insert_credential(
    conn: sqlite3.Connection,
    *,
    key_name: str,
    value_cipher: str,
    description: str | None,
    actor: str,
    at: str,
    scope: str | None = None,
    cred_type: str | None = None,
    expires_at: str | None = None,
) -> None:
    """插入新凭据（created_at = updated_at = at；D-37 v1.1 三列可选）。"""
    conn.execute(
        "INSERT INTO credentials (key_name, value_cipher, description, status, deleted_at, "
        "created_at, updated_at, updated_by, scope, cred_type, expires_at) "
        "VALUES (?,?,?,'active',NULL,?,?,?,?,?,?)",
        (key_name, value_cipher, description, at, at, actor, scope, cred_type, expires_at),
    )


def update_credential(
    conn: sqlite3.Connection,
    *,
    key_name: str,
    value_cipher: str,
    description: str | None,
    actor: str,
    at: str,
    scope: str | None = None,
    cred_type: str | None = None,
    expires_at: str | None = None,
) -> bool:
    """更新整行可写列（仅 active 行；D-37 v1.1：scope/cred_type/expires_at 一并写入）。

    合并语义（缺省=不变、空串=清空）在 operations 层完成，本层只收最终值。
    返回是否命中行。
    """
    cur = conn.execute(
        "UPDATE credentials SET value_cipher=?, description=?, updated_at=?, updated_by=?, "
        "scope=?, cred_type=?, expires_at=? WHERE key_name=? AND status='active'",
        (value_cipher, description, at, actor, scope, cred_type, expires_at, key_name),
    )
    return cur.rowcount > 0


def touch_last_used(conn: sqlite3.Connection, key_name: str, *, at: str) -> bool:
    """更新最近使用时间（D-37 v1.1；读成功解密后 best-effort 调用）。返回是否命中行。"""
    cur = conn.execute(
        "UPDATE credentials SET last_used_at=? WHERE key_name=? AND status='active'",
        (at, key_name),
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
