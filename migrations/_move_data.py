"""migrations/_move_data.py：v0.7 跨库存量数据搬运实现（D2：Python 双连接读写）。

T-427（2026-11-20）：场景层退役，本脚本只搬普通表（scenes / scene_vectors /
scene_memories / scene_versions），不再重建 scenes_fts（FTS 与触发器由退役
rename 脚本清理；回滚脚本负责重建）。

历史决策（保留说明）：不用 `ATTACH DATABASE`，因为外部内容 FTS5 跨库不会触发
目标库同步触发器且 `content_rowid='rowid'` 会漂移——本批已无 FTS 重建需求。

可重入性：所有写入均为 `INSERT OR IGNORE`，重复执行不产生重复行；
返回值给出每张表的「源行数 / 目标行数 / 本次新增」三元数据，供调用方核对搬运完整性。

⚠️ 本模块**不建任何新表、不补任何列**——目标库表结构由 `sgme/storage/db.py` 的
`SESSION_DDL` / `MEMORY_DDL` 在 `connect_*` 时保证就绪（§2.1.1 职责边界）。
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger("sgme.migrations.move_data")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """判断表是否存在于该连接对应的库中。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    """返回表行数；表不存在时返回 0。"""
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()[0])


def _move_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    columns: list[str],
) -> dict:
    """把 `src.table` 的指定列全量搬到 `dst.table`（`INSERT OR IGNORE`，可重入）。

    Args:
        src: 源库连接（旧 wiki.db）。
        dst: 目标库连接（memory.db 或 session.db，表结构已由 db.py 建好）。
        table: 表名（源库与目标库同名）。
        columns: 完整列名列表（**不使用 `SELECT *`**，避免列顺序漂移）。

    Returns:
        `{"table", "src_rows", "before", "after", "inserted", "skipped"}`。
        源表不存在时返回 `skipped=True`，其余计数为 0。
    """
    if not _table_exists(src, table):
        logger.info("源库无 %s 表，跳过搬运", table)
        return {"table": table, "src_rows": 0, "before": _count(dst, table),
                "after": _count(dst, table), "inserted": 0, "skipped": True}

    col_list = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    rows = src.execute(f"SELECT {col_list} FROM {table}").fetchall()
    before = _count(dst, table)
    dst.executemany(
        f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
        [tuple(r) for r in rows],
    )
    dst.commit()
    after = _count(dst, table)
    result = {
        "table": table,
        "src_rows": len(rows),
        "before": before,
        "after": after,
        "inserted": after - before,
        "skipped": False,
    }
    logger.info(
        "搬运 %s：源 %d 行，目标 %d → %d（新增 %d）",
        table, len(rows), before, after, after - before,
    )
    return result


def move(conn_dict: dict) -> dict:
    """执行 v0.7 全部跨库搬运，返回逐表摘要。

    Args:
        conn_dict: `{"memory": mem_conn, "session": session_conn, "wiki": legacy_wiki_conn}`。
            - `memory` / `session`：目标库连接，须已由 `db.py` 的 `connect_*` 建好表；
            - `wiki`：旧 wiki.db 连接（数据源），按 D5 保留归档、**不 DROP 任何表**。

    Returns:
        `{"tables": [逐表摘要...], "fts_rebuilt": bool, "fts_meta_merged": int}`。
    """
    src: sqlite3.Connection = conn_dict["wiki"]
    session: sqlite3.Connection = conn_dict["session"]
    mem: sqlite3.Connection = conn_dict["memory"]

    tables: list[dict] = []

    # 0) 目标库场景四表 + fts_meta：T-427 起不再由 connect_memory 启动建表，
    #    历史迁移需要显式建表（结构与退役前一致）。
    from sgme.data.db import SCENE_RETIRED_DDL
    from sgme.data.search import FTS_META_DDL
    mem.executescript(SCENE_RETIRED_DDL)
    mem.executescript(FTS_META_DDL)
    mem.commit()

    # 1) raw_files → session.db（普通表，rowid 无意义，直接 INSERT OR IGNORE）
    tables.append(_move_table(
        src, session, "raw_files",
        ["file_id", "path", "session_key", "agent_id", "started_at", "ended_at",
         "refined_at", "last_refined_seq", "status", "size", "content_hash"],
    ))

    # 2) scenes → memory.db（不保 rowid；content_seg 一并拷，步骤 6 rebuild 时按新 rowid 重绑）
    tables.append(_move_table(
        src, mem, "scenes",
        ["scene_id", "title", "content", "heat", "status", "created_at",
         "updated_at", "last_memory_added_at", "content_seg"],
    ))

    # 3) scene_vectors → memory.db（普通表，embedding 为 BLOB 列，无特殊约束）
    tables.append(_move_table(
        src, mem, "scene_vectors",
        ["scene_id", "embedding", "model", "dims", "embedded_at"],
    ))

    # 4) scene_memories → memory.db（普通表，PK(scene_id, memory_id)）
    tables.append(_move_table(
        src, mem, "scene_memories",
        ["scene_id", "memory_id"],
    ))

    # 5) scene_versions → memory.db（普通表，PK version_id）
    tables.append(_move_table(
        src, mem, "scene_versions",
        ["version_id", "scene_id", "content", "version_at", "reason"],
    ))

    # 6) scenes_fts 不再重建（T-427：场景层退役，FTS/触发器随四表退役清理）。
    fts_rebuilt = False

    # 7) fts_meta 合并（两库 key 不重叠：memory 侧 'segmenter'，wiki 侧 'segmenter_scenes'）
    #    仅当两库都存在 fts_meta 时补缺；场景 marker 属历史数据，保留不覆盖。
    fts_meta_merged = 0
    if _table_exists(src, "fts_meta") and _table_exists(mem, "fts_meta"):
        rows = src.execute("SELECT key, value FROM fts_meta").fetchall()
        before = _count(mem, "fts_meta")
        mem.executemany(
            "INSERT OR IGNORE INTO fts_meta (key, value) VALUES (?,?)",
            [tuple(r) for r in rows],
        )
        mem.commit()
        fts_meta_merged = _count(mem, "fts_meta") - before

    session.commit()
    mem.commit()

    return {
        "tables": tables,
        "fts_rebuilt": fts_rebuilt,
        "fts_meta_merged": fts_meta_merged,
    }
