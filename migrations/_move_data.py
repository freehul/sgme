"""migrations/_move_data.py：v0.7 跨库存量数据搬运实现（D2：Python 双连接读写）。

为什么不用 `ATTACH DATABASE`（D2 决策）：
1. `scenes_fts` 是 `content='scenes'` 的外部内容 FTS5 虚拟表，ATTACH 跨库不会触发目标库同步触发器；
2. `content_rowid='rowid'` 绑定源库 rowid，跨库搬运必然漂移，光拷数据必错。
   → 采用「不保 rowid + 搬完统一 `init_scenes_fts(mem)` rebuild」方案（见步骤 6）。

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

    # 6) scenes_fts 一次搞定（必须在 scenes 数据搬完之后调用）
    #    init_scenes_fts 内部一次性完成：建虚拟表 + 建 3 个触发器（scenes_ai/ad/au）
    #    + 回填 content_seg + `INSERT INTO scenes_fts(scenes_fts) VALUES('rebuild')`
    #    按**目标库当前 rowid** 重建索引。migrations 侧无需写任何一行 FTS 代码。
    fts_rebuilt = False
    try:
        from sgme.data.search import init_scenes_fts
        init_scenes_fts(mem)  # 注意实参是 mem，不是旧的 wiki_conn
        fts_rebuilt = True
    except Exception as e:
        # FTS 构建失败不阻断数据搬运（search_scenes 有 LIKE 兜底），但必须可观测
        logger.warning("scenes_fts 重建失败（数据已搬运，检索退化为 LIKE 兜底）：%s", e)

    # 7) fts_meta 合并（两库 key 不重叠：memory 侧 'segmenter'，wiki 侧 'segmenter_scenes'）
    #    放在步骤 6 之后：init_scenes_fts 已确保 memory.db 的 fts_meta 表存在，
    #    且已写入正确的 segmenter_scenes 值 → 这里用 INSERT OR IGNORE 只补缺、不覆盖。
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
