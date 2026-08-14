"""migrations/_registry.py：迁移版本注册表（有序迁移函数列表 + applied 标记）。

职责边界（§2.1.1）：
- 本注册表**只登记「存量数据搬运」类一次性迁移**，不建表、不补列（那归 `sgme/storage/db.py` 的 `*_DDL`）。
- applied 标记复用各库已有的 `schema_versions` 表（`version INTEGER PRIMARY KEY, name, applied_at`）。

版本号命名空间（避免与建表 schema 版本冲突）：
`sgme/storage/db.py` 的 `SCHEMA_VERSION` 历史取值为 1..4，未来仍会小步递增；
为了共用 `schema_versions` 表而互不撞主键，数据搬运类迁移一律使用
`MIGRATION_VERSION_BASE + 序号`（v0.7 首个迁移 0001 → 701）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

# 数据搬运类迁移的版本号基数（700 = v0.7 系列，与建表 schema 版本 1..4 隔离）
MIGRATION_VERSION_BASE = 700


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Migration:
    """一条一次性数据迁移。

    Attributes:
        version: 绝对版本号（`MIGRATION_VERSION_BASE + 序号`），写入 `schema_versions.version`。
        name: 迁移名（对应文件名，写入 `schema_versions.name`）。
        up: 迁移执行函数，签名 `up(conn_dict: dict) -> dict`，返回执行摘要。
        targets: 需要登记 applied 标记的库键名（`conn_dict` 中的键）。
    """

    version: int
    name: str
    up: Callable[[dict], dict]
    targets: tuple[str, ...] = ("memory", "session")


# 已注册迁移：(序号, 模块名, 登记 applied 标记的库)
# 新增迁移时在此追加一行即可，模块名须与 `migrations/` 下的文件名（去 .py）完全一致。
_REGISTERED: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "0001_split_three_dbs", ("memory", "session")),
)


def load_migrations() -> list[Migration]:
    """按版本号升序返回全部已注册迁移。

    迁移模块名以数字开头（`0001_...`），无法用 `import` 语句直接导入，
    故统一通过 `importlib.import_module` 动态加载（延迟到调用时，避免循环依赖）。
    """
    import importlib

    items: list[Migration] = []
    for seq, module_name, targets in _REGISTERED:
        mod = importlib.import_module(f"migrations.{module_name}")
        items.append(
            Migration(
                version=MIGRATION_VERSION_BASE + seq,
                name=module_name,
                up=mod.up,
                targets=targets,
            )
        )
    return sorted(items, key=lambda m: m.version)


def _ensure_schema_versions(conn: sqlite3.Connection) -> None:
    """确保 `schema_versions` 表存在（复用 storage 层 DDL 常量，不另写 DDL）。"""
    from sgme.data.db import SCHEMA_VERSIONS_DDL

    conn.executescript(SCHEMA_VERSIONS_DDL)
    conn.commit()


def is_applied(conn: sqlite3.Connection, version: int) -> bool:
    """判断指定版本是否已在该库登记为已应用。"""
    _ensure_schema_versions(conn)
    row = conn.execute(
        "SELECT version FROM schema_versions WHERE version=?", (version,)
    ).fetchone()
    return row is not None


def mark_applied(conn: sqlite3.Connection, version: int, name: str) -> None:
    """登记迁移已应用（幂等：已存在则不重复写）。"""
    _ensure_schema_versions(conn)
    if is_applied(conn, version):
        return
    conn.execute(
        "INSERT INTO schema_versions (version, name, applied_at) VALUES (?,?,?)",
        (version, name, _now_iso()),
    )
    conn.commit()
