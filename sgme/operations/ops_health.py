# -*- coding: utf-8 -*-
"""sgme/operations/ops_health.py：运维健康总览（只读，2026-11-04 B300）。

给 WebUI 运维页提供两块数据：
1. storage：memory.db / WAL / wiki.db / raw 文件与字节 / 备份快照与字节；
2. scenes：9.6 场景层下线就绪（四表行数、GONE/CONFLICT 规则数、compat 阶段与开关、
   ready + blockers + 人工动作清单）。

边界：只读（文件 stat / sqlite mode=ro），不改库、不删文件、不触发任何动作。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sgme import compat
from sgme.config import USER_ROOT, resolve_backup_dir
from sgme.scene_scan import scan_db

DEFAULT_DATA_DIR = USER_ROOT / "data"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _dir_stats(root: Path) -> tuple[int, int]:
    """返回 (文件数, 字节数)；目录不存在 → (0, 0)。"""
    if not root.is_dir():
        return 0, 0
    files = 0
    total = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        files += 1
        total += _size(p)
    return files, total


def _scene_rows(scan: dict) -> dict[str, int]:
    tables = scan.get("tables") or {}
    out: dict[str, int] = {}
    for name in ("scenes", "scene_vectors", "scenes_fts"):
        row = tables.get(name) or {}
        out[name] = int(row.get("rows") or 0) if row.get("exists") else 0
    return out


def _compat_flags(cfg: dict | None) -> tuple[bool, bool]:
    c = (cfg or {}).get("compat") if isinstance(cfg, dict) else None
    if not isinstance(c, dict):
        return False, False
    return bool(c.get("enforce")), bool(c.get("enforce_gone"))


def health_overview(cfg: dict | None = None) -> dict:
    """运维健康总览（只读）。"""
    paths = (cfg or {}).get("paths") if isinstance(cfg, dict) else None
    data_dir = Path((paths or {}).get("data_dir") or DEFAULT_DATA_DIR)
    raw_dir = Path((paths or {}).get("raw_dir") or (data_dir / "raw"))
    backup_dir = resolve_backup_dir(cfg)

    mem_db = data_dir / "memory.db"
    wiki_db = data_dir / "wiki.db"
    raw_files, raw_bytes = _dir_stats(raw_dir)
    backup_snapshots = 0
    if backup_dir.is_dir():
        backup_snapshots = sum(
            1 for d in backup_dir.iterdir() if d.is_dir() and (d / "memory.db").is_file()
        )
    _, backup_bytes = _dir_stats(backup_dir)

    scan = scan_db(data_dir)
    rows = _scene_rows(scan)
    phase = compat.compat_phase(cfg)
    enforce, enforce_gone = _compat_flags(cfg)
    gone_rules = len(compat.GONE_RULES)
    conflict_rules = len(compat.CONFLICT_RULES)

    blockers: list[str] = []
    if not scan.get("available"):
        blockers.append("memory.db 不可用（数据目录未初始化或不可读）")
    if gone_rules <= 0:
        blockers.append("GONE_RULES 为空，无 410 规则可执行")
    if phase.get("phase") == "pre_stable":
        blockers.append("兼容期未开始（compat.stable_since 未设置）")
    elif phase.get("phase") == "unknown":
        blockers.append("compat.stable_since 非法，阶段无法判定")

    ready = (
        bool(scan.get("available"))
        and gone_rules > 0
        and phase.get("phase") in ("active", "expired")
    )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "storage": {
            "data_dir": str(data_dir),
            "memory_db_bytes": _size(mem_db),
            "memory_wal_bytes": _size(data_dir / "memory.db-wal"),
            "wiki_db_bytes": _size(wiki_db),
            "raw_files": raw_files,
            "raw_bytes": raw_bytes,
            "backup_snapshots": backup_snapshots,
            "backup_bytes": backup_bytes,
        },
        "scenes": {
            "scenes": rows["scenes"],
            "scene_vectors": rows["scene_vectors"],
            "scenes_fts": rows["scenes_fts"],
            "gone_rules": gone_rules,
            "conflict_rules": conflict_rules,
            "compat_phase": phase.get("phase"),
            "stable_since": phase.get("stable_since"),
            "days_left": phase.get("days_left"),
            "enforce": enforce,
            "enforce_gone": enforce_gone,
            "ready": ready,
            "blockers": blockers,
        },
        "manual_actions": [
            "四表冻结（生产置只读 + deprecated_2_0 标记，不删行）",
            "摘除 POST /v1/admin/scenes/{id}/status 与 POST /v1/admin/scene-gc/trigger（或开 enforce_gone）",
            "按下线清单顺序执行后跑 e2e_smoke + 9.4 #9/#10",
        ],
    }
