"""backup/manager.py：备份恢复管理器（§17）。

职责：
- create_snapshot：SQLite backup API 一致快照（免停机）+ 原始层复制
- rotate_snapshots：按 level 分类的轮转清理
- push_remote：异地副本推送（失败不阻塞本地备份）
- archive_raw_cold：>N 天原始文件冷归档（zstd 优先，降级 gzip）
- restore：恢复前自动再备份 + 覆盖恢复
- verify_integrity：溯源链完整性校验（memories → sources → raw_files）

快照目录结构：dest_dir/{snapshot_id}/{memory.db, session.db, wiki.db, raw/}
snapshot_id 格式：{level}_{YYYYMMDD_HHMMSS}_{uuid8}

v0.7 三库拆分：raw_files 迁入 session.db，scenes 系列迁入 memory.db，
故快照/恢复/完整性校验均按三库口径（memory.db + session.db + wiki.db）。
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sgme import config as sgme_config
from sgme.data import db as db_mod

# 三库文件名（快照/恢复统一口径，顺序 = (memory, session, wiki)）
DB_FILENAMES: tuple[str, str, str] = ("memory.db", "session.db", "wiki.db")


# ---------- 内部工具 ----------


def _resolve_snapshot_sources(
    conn_pair: tuple[sqlite3.Connection, ...] | None,
) -> tuple[
    sqlite3.Connection | None,
    sqlite3.Connection | None,
    sqlite3.Connection | None,
]:
    """把调用方传入的连接元组归一为 `(mem, session, wiki)`，缺位返回 None。

    - 三元组 `(mem, session, wiki)`：v0.7 三库口径，原样返回
    - 二元组 `(mem, wiki)`：v0.7 前旧口径，session 位留空由调用方兜底
    - None / 空元组：三位全空
    - 其余长度视为非法，抛 ValueError（早失败优于静默备错库）
    """
    if not conn_pair:
        return None, None, None
    conns = tuple(conn_pair)
    if len(conns) == 3:
        return conns[0], conns[1], conns[2]
    if len(conns) == 2:
        # 兼容旧二元组 (mem_conn, wiki_conn)
        return conns[0], None, conns[1]
    raise ValueError(f"conn_pair 长度必须为 2 或 3，得到 {len(conns)}")

def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backup_db(src_conn: sqlite3.Connection, dst_path: Path) -> None:
    """用 SQLite backup API 做一致快照（WAL 模式下安全）。

    dst_path 的父目录必须已存在。

    为什么这里直连 sqlite3 而非走 storage.db.connect_*（B30 边界说明）：
    SQLite backup API 需要原生 Connection 对象；connect_* 会跑迁移链
    （_ensure_schema/_migrate_*），备份场景打开源库绝不能触发迁移副作用。
    故保持裸连接（只读打开，不迁移），这是唯一允许绕过 data 层的场景。
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_conn = sqlite3.connect(str(dst_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()


def _copy_raw_full(src_raw: Path, dst_raw: Path) -> list[str]:
    """全量复制 raw/ 目录，返回复制的相对路径列表。"""
    if not src_raw.exists():
        return []
    dst_raw.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for f in src_raw.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src_raw)
            dst_f = dst_raw / rel
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst_f)
            copied.append(str(rel))
    return copied


def _copy_raw_incremental(src_raw: Path, dst_raw: Path) -> list[str]:
    """增量复制：仅当日（本地时区）新增/变更文件（mtime > 今日 00:00）。"""
    if not src_raw.exists():
        return []
    dst_raw.mkdir(parents=True, exist_ok=True)
    # 今日 00:00 的 epoch 时间戳
    today_midnight = datetime.combine(
        datetime.now().date(), datetime.min.time()
    ).timestamp()
    copied: list[str] = []
    for f in src_raw.rglob("*"):
        if f.is_file() and f.stat().st_mtime > today_midnight:
            rel = f.relative_to(src_raw)
            dst_f = dst_raw / rel
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst_f)
            copied.append(str(rel))
    return copied


def _compress_file(src: Path, dst: Path) -> Path:
    """压缩单个文件，优先 zstd，降级 gzip。"""
    try:
        import zstandard
        cctx = zstandard.ZstdCompressor()
        data = src.read_bytes()
        dst.write_bytes(cctx.compress(data))
    except ImportError:
        # zstd 不可用，降级 gzip
        data = src.read_bytes()
        with gzip.open(str(dst), "wb") as f:
            f.write(data)
    return dst


# ---------- 公开 API ----------

def create_snapshot(
    data_dir: str | Path,
    dest_dir: str | Path,
    level: str = "incremental",
    conn_pair: tuple[sqlite3.Connection, ...] | None = None,
    id_prefix: str | None = None,
) -> dict:
    """创建一致快照。

    - 使用 SQLite backup API（conn.backup(dst)）做免停机一致快照
    - level='incremental'：三库全量快照 + 原始层仅复制当日新增/变更文件
    - level='full'/'monthly'：三库全量 + 原始层全量复制
    - 返回 {snapshot_id, level, path, created_at, files}
    - snapshot_id 格式：{id_prefix}{level}_{YYYYMMDD_HHMMSS}_{uuid8}

    conn_pair（v0.7 起语义扩展，保留旧参数名以兼容既有调用方）：
    - 三元组 `(mem_conn, session_conn, wiki_conn)`：推荐口径
    - 二元组 `(mem_conn, wiki_conn)`：v0.7 前旧口径，session.db 走临时裸连接兜底
    - None：三库全部走临时裸连接
    """
    data_dir = Path(data_dir)
    dest_dir = Path(dest_dir)

    # 生成 snapshot_id
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:8]
    prefix = id_prefix or ""
    snapshot_id = f"{prefix}{level}_{ts}_{uid}"
    snap_dir = dest_dir / snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    # 三库一致快照（SQLite backup API）
    mem_src, session_src, wiki_src = _resolve_snapshot_sources(conn_pair)

    # 未提供的连接走临时裸连接兜底
    # 源库直连（非 storage.connect_*）：backup API 需要原生 Connection，
    # 且绝不能触发 connect_* 的迁移链（备份期间改库结构 = 灾难）。
    # 唯一允许绕过 data 层的场景，见 _backup_db 注释（B30）。
    own_conns: list[sqlite3.Connection] = []
    if mem_src is None:
        mem_src = sqlite3.connect(str(data_dir / "memory.db"))
        own_conns.append(mem_src)
    if session_src is None:
        session_src = sqlite3.connect(str(data_dir / "session.db"))
        own_conns.append(session_src)
    if wiki_src is None:
        wiki_src = sqlite3.connect(str(data_dir / "wiki.db"))
        own_conns.append(wiki_src)

    try:
        _backup_db(mem_src, snap_dir / "memory.db")
        _backup_db(session_src, snap_dir / "session.db")
        _backup_db(wiki_src, snap_dir / "wiki.db")
    finally:
        for c in own_conns:
            try:
                c.close()
            except Exception:
                pass

    # 原始层复制（原始层位于项目根 raw/，通过 sgme_config.RAW_DIR 引用）
    raw_src = Path(sgme_config.RAW_DIR)
    snap_raw = snap_dir / "raw"
    if level == "incremental":
        _copy_raw_incremental(raw_src, snap_raw)
    else:
        # full / monthly：全量复制
        _copy_raw_full(raw_src, snap_raw)

    # 收集快照内文件列表（相对路径）
    files = sorted(
        str(f.relative_to(snap_dir))
        for f in snap_dir.rglob("*")
        if f.is_file()
    )

    return {
        "snapshot_id": snapshot_id,
        "level": level,
        "path": str(snap_dir),
        "created_at": _now_iso(),
        "files": files,
    }


def rotate_snapshots(
    backup_dir: str | Path,
    keep_full: int = 7,
    keep_monthly: int | None = None,
) -> dict:
    """按 level 分类轮转快照，保留最近 N 份。

    - full：保留最近 keep_full 份
    - monthly：keep_monthly=None 表示不轮转（∞）
    - incremental / pre_restore_*：不轮转（全部保留）
    - 返回 {removed: [...], kept: [...]}
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return {"removed": [], "kept": []}

    # 按 level 分类收集快照目录
    # snapshot_id 格式：{prefix}{level}_{YYYYMMDD_HHMMSS}_{uuid8}
    # prefix 可为 "pre_restore_"
    categories: dict[str, list[Path]] = {}
    for d in backup_dir.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        # pre_restore_ 前缀的快照不轮转
        if name.startswith("pre_restore_"):
            continue
        if name.startswith("incremental_"):
            cat = "incremental"
        elif name.startswith("full_"):
            cat = "full"
        elif name.startswith("monthly_"):
            cat = "monthly"
        else:
            continue
        categories.setdefault(cat, []).append(d)

    removed: list[str] = []
    kept: list[str] = []

    for cat, dirs in categories.items():
        # 按名称降序排序（名称含时间戳，字典序 = 时间序）
        dirs.sort(key=lambda p: p.name, reverse=True)

        if cat == "full":
            limit = keep_full
        elif cat == "monthly":
            limit = keep_monthly  # None = 全部保留
        else:
            # incremental 不轮转
            limit = None

        if limit is None:
            # 不轮转，全部保留
            kept.extend(d.name for d in dirs)
            continue

        # 保留最近 limit 份，删除其余
        for i, d in enumerate(dirs):
            if i < limit:
                kept.append(d.name)
            else:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)

    return {"removed": removed, "kept": kept}


def push_remote(snapshot_path: str | Path, remote_dir: str | Path | None) -> dict:
    """推送快照到远程目录（NAS 挂载/异机目录）。

    - 失败仅返回 {ok: False, error: ...}，不抛异常（不阻塞本地备份）
    - remote_dir 为 None 时跳过返回 {ok: True, skipped: True}
    - 成功返回 {ok: True, remote_path: ...}
    """
    if remote_dir is None:
        return {"ok": True, "skipped": True}

    try:
        snapshot_path = Path(snapshot_path)
        remote_dir = Path(remote_dir)
        remote_dir.mkdir(parents=True, exist_ok=True)
        dst = remote_dir / snapshot_path.name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(snapshot_path, dst)
        return {"ok": True, "remote_path": str(dst)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def archive_raw_cold(
    raw_dir: str | Path,
    archive_dir: str | Path,
    days: int = 90,
) -> dict:
    """>days 天未修改的 .md 文件压缩为冷归档。

    - zstd 不可用时降级 gzip
    - 归档后原文件保留（仍参与溯源）
    - 返回 {archived_count, files}
    """
    raw_dir = Path(raw_dir)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    if not raw_dir.exists():
        return {"archived_count": 0, "files": []}

    cutoff = time.time() - days * 86400
    archived: list[str] = []

    # 判断 zstd 是否可用
    use_zstd = False
    try:
        import zstandard  # noqa: F401
        use_zstd = True
    except ImportError:
        pass

    for md_file in raw_dir.rglob("*.md"):
        if not md_file.is_file():
            continue
        if md_file.stat().st_mtime >= cutoff:
            continue
        # 压缩为冷归档
        ext = ".zst" if use_zstd else ".gz"
        dst = archive_dir / (md_file.name + ext)
        _compress_file(md_file, dst)
        archived.append(str(dst))

    return {"archived_count": len(archived), "files": archived}


def restore(
    snapshot_path: str | Path,
    data_dir: str | Path,
    raw_dir: str | Path,
    conn_pair: tuple[sqlite3.Connection, ...] | None = None,
) -> dict:
    """从快照恢复。

    - 恢复前自动再备份当前状态（snapshot_id 前缀 pre_restore_）
    - 关闭当前 conn（若提供）→ 覆盖 data_dir 下 db 文件 → 恢复 raw/ → 重开 conn
    - 返回 {restored: {...}, pre_restore_snapshot: ...}
    - 结果中 _new_conns 字段包含重开后的三元组
      `(mem_conn, session_conn, wiki_conn)`，供调用方更新引用
    - conn_pair 语义同 `create_snapshot`（三元组优先，兼容旧二元组）
    """
    snapshot_path = Path(snapshot_path)
    data_dir = Path(data_dir)
    raw_dir = Path(raw_dir)

    # 1. 恢复前自动备份当前状态（pre_restore_ 前缀）
    pre_backup_dir = snapshot_path.parent
    pre_snap = create_snapshot(
        data_dir=data_dir,
        dest_dir=pre_backup_dir,
        level="full",
        conn_pair=conn_pair,
        id_prefix="pre_restore_",
    )

    # 2. 关闭当前 conn
    if conn_pair:
        for c in conn_pair:
            try:
                c.close()
            except Exception:
                pass

    # 3. 覆盖 data_dir 下 db 文件
    restored_files: list[str] = []
    data_dir.mkdir(parents=True, exist_ok=True)
    for db_name in DB_FILENAMES:
        src_db = snapshot_path / db_name
        dst_db = data_dir / db_name
        if src_db.exists():
            # 删除可能残留的 WAL/SHM 文件（关闭连接后通常已清理，此处兜底）
            for suffix in ("-wal", "-shm"):
                wal_file = data_dir / (db_name + suffix)
                if wal_file.exists():
                    wal_file.unlink()
            shutil.copy2(src_db, dst_db)
            restored_files.append(db_name)

    # 4. 恢复 raw/
    src_raw = snapshot_path / "raw"
    if src_raw.exists():
        raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(raw_dir, ignore_errors=True)
        shutil.copytree(src_raw, raw_dir)
        restored_files.append("raw/")

    # 5. 重开 conn（v0.7：三库）
    new_mem = db_mod.connect_memory(data_dir)
    new_session = db_mod.connect_session(data_dir)
    new_wiki = db_mod.connect_wiki(data_dir)

    return {
        "restored": {
            "files": restored_files,
            "snapshot_id": snapshot_path.name,
        },
        "pre_restore_snapshot": pre_snap["snapshot_id"],
        "_new_conns": (new_mem, new_session, new_wiki),
    }


def verify_integrity(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
) -> dict:
    """校验溯源链完整性：memories → memory_sources.source_ref → raw_files.file_id。

    - source_ref 格式为 {file_id}:{seq}，提取 file_id 部分校验
    - v0.7：raw_files 已迁入 session.db，故第二个参数是 session_conn
    - 返回 {ok, broken_count, broken_samples}
    """
    # 获取所有 raw_files.file_id
    file_ids = {
        r["file_id"]
        for r in session_conn.execute("SELECT file_id FROM raw_files").fetchall()
    }

    # 检查每条 source_ref 是否指向存在的 file_id
    rows = mem_conn.execute(
        "SELECT memory_id, source_ref FROM memory_sources"
    ).fetchall()

    broken: list[dict] = []
    for r in rows:
        ref = r["source_ref"]
        # source_ref 格式：{file_id}:{seq}，提取 file_id
        file_id = ref.split(":")[0] if ":" in ref else ref
        if file_id not in file_ids:
            broken.append({"memory_id": r["memory_id"], "source_ref": ref})

    return {
        "ok": len(broken) == 0,
        "broken_count": len(broken),
        "broken_samples": broken[:10],
    }
