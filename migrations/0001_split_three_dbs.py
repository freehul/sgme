"""migrations/0001_split_three_dbs.py：v0.7 双库 → 三库存量数据搬家（一次性、可重入）。

四项职责（严格按序，任一前置失败即中止，不做部分搬运）：
1. **迁移前自动快照** —— 复用 `sgme/backup/manager.py` 的 `create_snapshot()`，
   落到 `data/backups/pre_v07/`；**备份失败立即中止**（B5：勿新写备份逻辑）。
2. **raw_files → session.db**（旧 wiki.db 为源，D5 保留归档不 DROP）。
3. **scenes / scene_vectors / scene_memories / scene_versions → memory.db**，
   随后调 `init_scenes_fts(mem_conn)` 按新 rowid 重建 `scenes_fts`。
4. **删除 `data/sgme.db`**（A1：0 字节历史误建空文件，代码零引用），
   仅在备份成功 + 搬运完成 + 溯源校验不劣化后执行。

⚠️ 本脚本**不建任何新表、不补任何列**（那归 `sgme/storage/db.py` 的 `*_DDL`，§2.1.1）。

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ 🔥 真实迁移前必读：只复制 `.db` 不带 `-wal` 会丢约 1% 数据（已踩坑）        ║
# ╠══════════════════════════════════════════════════════════════════════════╣
# SGME Server 是长驻进程（默认占用 9910 端口），持有约 4MB 未 checkpoint 的
# WAL。QA 在副本演练时**只拷了 `.db` 文件**，结果行数比正确值少了约 1%：
#   错误值：raw_files 461 / scenes 134 / scene_memories 11844 / scene_versions 1007
#   正确值：raw_files 467 / scenes 138 / scene_memories 11917 / scene_versions 1014
# 那段最新数据还在 WAL 里，没落进主库文件。迁移前务必做到以下任一点：
#   1) **先停 SGME Server**（占用 9910 端口的进程）再迁移，否则源库 WAL 未 checkpoint；
#   2) 若需在副本上演练，必须连同 `-wal` / `-shm` 一起复制，
#      或对源库先执行 `PRAGMA wal_checkpoint(TRUNCATE)` 强制落盘；
#   3) 迁移后用下列**正确行数基线**逐表比对，发现偏差立即回滚快照：
#      raw_files=467 / scenes=138 / scene_memories=11917 /
#      scene_versions=1014 / scene_vectors=109。
# ╚══════════════════════════════════════════════════════════════════════════╝

关于溯源校验的口径（实现说明）：
`verify_integrity()` 校验 `memories → memory_sources.source_ref → raw_files.file_id`。
生产库可能**本来就存在**历史断链（如原始文件被清理），若要求「必须 ok=True 才继续」
会导致迁移永远无法执行。故采用**基线比对**：搬运前先在旧 wiki.db 上取 baseline
`broken_count`，搬运后在 session.db 上复校，只有「断链数不增加」才允许删除 `sgme.db`。
这既落实了「校验通过才清理」的意图，又不会被存量数据质量问题卡死。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("sgme.migrations.0001")

# 迁移前快照落点（相对 data_dir）
PRE_SNAPSHOT_SUBDIR = "backups/pre_v07"


class MigrationAbort(RuntimeError):
    """迁移前置条件失败（备份失败等）——立即中止，不执行任何搬运。"""


def _pre_backup(data_dir: Path, conn_dict: dict) -> dict:
    """迁移前自动快照（复用 backup/manager.create_snapshot，勿新写备份逻辑）。

    Raises:
        MigrationAbort: 快照创建失败或产物缺失 —— 中止迁移。
    """
    from sgme.backup import manager as backup_mod

    dest = data_dir / PRE_SNAPSHOT_SUBDIR
    try:
        snap = backup_mod.create_snapshot(
            data_dir=data_dir,
            dest_dir=dest,
            level="full",
            # conn_pair 自 v0.7 起接受三元组 (memory, session, wiki)，
            # 参数名保留以兼容既有调用方（见 backup/manager.create_snapshot 文档）
            conn_pair=(conn_dict["memory"], conn_dict["session"], conn_dict["wiki"]),
            id_prefix="pre_v07_",
        )
    except Exception as e:
        raise MigrationAbort(f"迁移前备份失败，已中止（未做任何搬运）：{e}") from e

    snap_dir = Path(snap["path"])
    missing = [n for n in ("memory.db", "wiki.db") if not (snap_dir / n).exists()]
    if missing:
        raise MigrationAbort(f"迁移前备份产物缺失 {missing}，已中止（未做任何搬运）")

    logger.info("迁移前快照已创建：%s", snap["snapshot_id"])
    return snap


def _baseline_broken_count(mem_conn, legacy_wiki_conn) -> int:
    """搬运前的溯源断链基线（在旧 wiki.db 的 raw_files 上统计）。"""
    from sgme.backup import manager as backup_mod

    try:
        return int(backup_mod.verify_integrity(mem_conn, legacy_wiki_conn)["broken_count"])
    except Exception as e:
        logger.warning("基线溯源校验未能完成（按 -1 处理，将跳过 sgme.db 清理）：%s", e)
        return -1


def _drop_legacy_sgme_db(data_dir: Path) -> dict:
    """删除历史误建的空文件 `data/sgme.db`（A1）。

    安全阀：仅当文件存在**且大小为 0 字节**时删除；非空一律保留并告警，
    避免误删任何可能含数据的库文件。
    """
    p = data_dir / "sgme.db"
    if not p.exists():
        return {"removed": False, "reason": "not_exists"}
    size = p.stat().st_size
    if size != 0:
        logger.warning("data/sgme.db 非空（%d 字节），出于安全未删除，请人工确认", size)
        return {"removed": False, "reason": f"not_empty:{size}"}
    p.unlink()
    logger.info("已删除历史空文件 data/sgme.db")
    return {"removed": True, "reason": "empty_file"}


def up(conn_dict: dict) -> dict:
    """执行本次迁移。

    Args:
        conn_dict: `{"data_dir": Path, "memory": conn, "session": conn, "wiki": conn}`。
            三个连接均由 `sgme/storage/db.py` 的 `connect_*` 提供（表结构已就绪）；
            `wiki` 同时充当旧 wiki.db 的数据源（raw_files / scenes 系列仍在其中）。

    Returns:
        执行摘要 `{"pre_snapshot", "baseline_broken", "moved", "post_broken", "sgme_db"}`。

    Raises:
        MigrationAbort: 迁移前备份失败。
    """
    from sgme.backup import manager as backup_mod
    from migrations import _move_data

    data_dir = Path(conn_dict["data_dir"])
    mem = conn_dict["memory"]
    session = conn_dict["session"]
    wiki = conn_dict["wiki"]

    # 1) 迁移前自动备份（失败即中止）
    snap = _pre_backup(data_dir, conn_dict)

    # 2) 溯源断链基线（旧库口径）
    baseline = _baseline_broken_count(mem, wiki)

    # 3) 跨库搬运 + scenes_fts 重建
    moved = _move_data.move({"memory": mem, "session": session, "wiki": wiki})

    # 4) 搬运后复校（新库口径：raw_files 已在 session.db）
    try:
        post = backup_mod.verify_integrity(mem, session)
        post_broken = int(post["broken_count"])
    except Exception as e:
        logger.warning("搬运后溯源校验未能完成：%s", e)
        post_broken = -1

    # 5) 清理历史空文件 sgme.db（仅在校验不劣化时执行）
    if baseline >= 0 and post_broken >= 0 and post_broken <= baseline:
        sgme_db = _drop_legacy_sgme_db(data_dir)
    else:
        logger.warning(
            "溯源校验劣化或未完成（baseline=%s, post=%s），跳过 data/sgme.db 清理",
            baseline, post_broken,
        )
        sgme_db = {"removed": False, "reason": f"verify_guard:{baseline}->{post_broken}"}

    return {
        "pre_snapshot": snap["snapshot_id"],
        "baseline_broken": baseline,
        "moved": moved,
        "post_broken": post_broken,
        "sgme_db": sgme_db,
    }
