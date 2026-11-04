# -*- coding: utf-8 -*-
"""scripts/purged_copies_report.py：已彻底删除记忆的剩余副本报告（D-44，只读）。

背景：D-44 彻底删除只清活库；备份快照（最长 30 天）与 L0 原始层（raw/）仍可能
留有已删内容。本脚本**只报告**这些剩余副本，不写库、不改文件、不删任何东西——
清理动作分别由 scripts/purge_backups.py（快照）与人工 redact（raw/）完成。

数据源：
- 默认：活库 memory.db 的 deletion_audit（mode='purge'，排除 __backup_purge__ 哨兵）；
- 或 --memory-id 显式指定（可重复 / 逗号分隔），不依赖审计表。

报告内容（逐条 id）：
- 活库 memories / memory_archive / memory_vectors 是否仍有行（给出计数）；
- 哪些备份快照 <backup_dir>/<snapshot>/memory.db 仍含该 id；
- raw/ 原始层文件总数（L0 按会话整文件存储，**无法按段落定位**到单条记忆，
  redact 需人工）。

复用：
- scripts/purge_backups.py 的 purged_ids() / scan_snapshot_db()（只读，同口径）；
- sgme.config.DATA_DIR / resolve_backup_dir() 解析数据目录与备份目录。

用法：
  .venv\\Scripts\\python.exe scripts\\purged_copies_report.py
  .venv\\Scripts\\python.exe scripts\\purged_copies_report.py --json
  .venv\\Scripts\\python.exe scripts\\purged_copies_report.py --memory-id <id> --data-dir <目录>
  .venv\\Scripts\\python.exe scripts\\purged_copies_report.py --backup-dir <快照目录>

退出码：0 = 报告生成成功；2 = 参数错误。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:  # 脚本模式 / 测试按路径加载时都要能 import 同目录脚本
    sys.path.insert(0, str(SCRIPTS_DIR))

# 复用 purge_backups 的只读口径（该模块顶层无副作用，只定义函数/常量）
import purge_backups

ACTIVE_TABLES = ("memories", "memory_archive", "memory_vectors")


def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK：中文/emoji 直接 print 会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _ro(db_path: Path) -> sqlite3.Connection:
    """只读打开（mode=ro，绝不创建/修改库文件）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _count_active_rows(data_dir: Path, ids: list[str]) -> tuple[dict, list[str]]:
    """活库里每个 id 在 memories / memory_archive / memory_vectors 的行数。

    返回 ({memory_id: {table: rows}}, warnings)。库缺失只告警，不建库。
    """
    counts = {mid: {table: 0 for table in ACTIVE_TABLES} for mid in ids}
    warnings: list[str] = []
    db_path = Path(data_dir) / "memory.db"
    if not db_path.exists():
        warnings.append(f"活库不存在：{db_path}（活库残留项一律按 0 报告）")
        return counts, warnings
    if not ids:
        return counts, warnings
    conn = _ro(db_path)
    try:
        marks = ",".join("?" * len(ids))
        for table in ACTIVE_TABLES:
            if not _has_table(conn, table):
                warnings.append(f"活库缺表 {table}（按 0 报告）")
                continue
            rows = conn.execute(
                f"SELECT memory_id, COUNT(*) c FROM {table} "
                f"WHERE memory_id IN ({marks}) GROUP BY memory_id",
                tuple(ids),
            ).fetchall()
            for row in rows:
                counts[row["memory_id"]][table] = int(row["c"])
    finally:
        conn.close()
    return counts, warnings


def _snapshot_ids(db_path: Path, ids: list[str]) -> set[str]:
    """快照库里实际命中的 id 集合（memories + memory_archive，同 purge 口径）。

    scan_snapshot_db() 只给聚合行数；逐 id 明细需要这一遍查询。
    """
    if not ids or not db_path.exists():
        return set()
    conn = _ro(db_path)
    try:
        hit: set[str] = set()
        marks = ",".join("?" * len(ids))
        for table in ("memories", "memory_archive"):
            if not _has_table(conn, table):
                continue
            rows = conn.execute(
                f"SELECT DISTINCT memory_id FROM {table} WHERE memory_id IN ({marks})",
                tuple(ids),
            ).fetchall()
            hit.update(str(row["memory_id"]) for row in rows)
        return hit
    finally:
        conn.close()


def _count_files(path: Path) -> int:
    """递归统计文件数（不含目录）；目录不存在返回 0。"""
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


def resolve_raw_dir(data_dir: Path, raw_dir: str | Path | None = None) -> Path:
    """解析 L0 原始层目录。

    候选顺序：显式 raw_dir → <data_dir>/raw → <data_dir 同级>/raw（T-23 标准布局：
    $SGME_HOME/data 与 $SGME_HOME/raw 平级）；都不存在时返回同级 raw/ 并如实报 0。
    """
    if raw_dir:
        return Path(raw_dir)
    inside = Path(data_dir) / "raw"
    if inside.exists():
        return inside
    sibling = Path(data_dir).parent / "raw"
    if sibling.exists():
        return sibling
    return sibling


def _scan_snapshots(backup_dir: Path, ids: list[str]) -> tuple[list[dict], list[str]]:
    """扫描备份快照：聚合命中（复用 scan_snapshot_db）+ 逐 id 明细 + raw 文件数。"""
    snapshots: list[dict] = []
    warnings: list[str] = []
    if not backup_dir.exists():
        warnings.append(f"备份目录不存在：{backup_dir}（快照残留项一律按空报告）")
        return snapshots, warnings
    for entry in sorted(backup_dir.iterdir(), key=lambda p: p.name):
        db_path = entry / "memory.db"
        if not entry.is_dir() or not db_path.exists():
            continue
        # 复用 purge_backups 的只读聚合口径（与清理脚本报告一致）
        scan = purge_backups.scan_snapshot_db(db_path, ids)
        hit_ids = _snapshot_ids(db_path, ids)
        snapshots.append(
            {
                "name": entry.name,
                "path": str(db_path),
                "available": bool(scan.get("available")),
                "matched_rows": int(scan.get("matched") or 0),
                "tables": scan.get("tables") or {},
                "ids_in_snapshot": sorted(hit_ids),
                "raw_files": _count_files(entry / "raw"),
            }
        )
    return snapshots, warnings


def build_report(
    data_dir: str | Path,
    backup_dir: str | Path,
    *,
    memory_ids: list[str] | None = None,
    raw_dir: str | Path | None = None,
) -> dict:
    """生成报告（纯只读：所有库连接均为 mode=ro，不写文件）。"""
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)

    warnings: list[str] = []
    if memory_ids:
        # 显式指定：去空白、去重、排序，保证报告稳定
        ids = sorted({str(mid).strip() for mid in memory_ids if str(mid).strip()})
        source = "explicit"
        if not ids:
            warnings.append("--memory-id 未提供有效 id")
    else:
        ids = sorted(purge_backups.purged_ids(data_dir))
        source = "deletion_audit"
        if not ids:
            warnings.append(
                "deletion_audit 中没有 mode='purge' 的记录（已排除 __backup_purge__ 哨兵）；"
                "可用 --memory-id 显式指定"
            )

    active_counts, active_warnings = _count_active_rows(data_dir, ids)
    warnings.extend(active_warnings)

    snapshots, snap_warnings = _scan_snapshots(backup_dir, ids)
    warnings.extend(snap_warnings)

    resolved_raw_dir = resolve_raw_dir(data_dir, raw_dir)
    raw_files = _count_files(resolved_raw_dir)
    if not resolved_raw_dir.exists():
        warnings.append(
            f"原始层目录不存在：{resolved_raw_dir}（raw 文件数按 0 报告）"
        )

    # 逐 id：活库计数 + 命中快照名
    records = []
    for mid in ids:
        counts = active_counts.get(mid) or {table: 0 for table in ACTIVE_TABLES}
        active_any = any(counts.get(table, 0) > 0 for table in ACTIVE_TABLES)
        records.append(
            {
                "memory_id": mid,
                "active": {table: int(counts.get(table, 0)) for table in ACTIVE_TABLES},
                "active_any": active_any,
                "snapshots_with_id": sorted(
                    snap["name"] for snap in snapshots if mid in snap["ids_in_snapshot"]
                ),
            }
        )

    ids_still_active = sum(1 for record in records if record["active_any"])
    ids_in_snapshots = sum(1 for record in records if record["snapshots_with_id"])
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "read_only": True,
        "data_dir": str(data_dir.resolve()),
        "backup_dir": str(backup_dir.resolve()),
        "source": source,
        "memory_ids": ids,
        "records": records,
        "snapshots": snapshots,
        "raw_layer": {
            "dir": str(resolved_raw_dir.resolve()),
            "files": raw_files,
            "note": (
                "raw/ 是 L0 原始层（按会话整文件存储），无法按段落定位到单条记忆；"
                "如需彻底清除，请人工 redact 对应文件（本脚本不删/不改）"
            ),
        },
        "summary": {
            "ids": len(ids),
            "ids_still_active": ids_still_active,
            "ids_in_snapshots": ids_in_snapshots,
            "snapshots_scanned": len(snapshots),
            "snapshots_with_hits": sum(
                1 for snap in snapshots if snap["ids_in_snapshot"]
            ),
        },
        "warnings": warnings,
        "notes": [
            "本脚本只读：不写活库/快照、不改 raw 文件、不调用任何清理接口",
            "快照清理用 scripts/purge_backups.py（默认 dry-run）；raw redact 为人工步骤",
        ],
    }


def render_text(report: dict) -> str:
    """人类可读报告。"""
    summary = report["summary"]
    source_label = (
        "deletion_audit（mode='purge'，已排除 __backup_purge__ 哨兵）"
        if report["source"] == "deletion_audit"
        else "--memory-id 显式指定"
    )
    lines = [
        "=" * 78,
        "已彻底删除记忆的剩余副本报告（只读 · 不写库 / 不改文件 / 不删文件）",
        "=" * 78,
        f"数据目录：{report['data_dir']}",
        f"备份快照：{report['backup_dir']}（扫描 {summary['snapshots_scanned']} 个）",
        f"原始层：{report['raw_layer']['dir']}（{report['raw_layer']['files']} 个文件）",
        f"ID 来源：{source_label}，共 {summary['ids']} 个",
        "",
        "【逐条剩余副本】",
    ]
    if not report["records"]:
        lines.append("  （无待报告 id）")
    for record in report["records"]:
        active = record["active"]
        verdict = "活库仍有残留" if record["active_any"] else "活库无残留"
        lines.append(f"- {record['memory_id']}")
        lines.append(
            f"    活库：memories={active['memories']}｜"
            f"memory_archive={active['memory_archive']}｜"
            f"memory_vectors={active['memory_vectors']} → {verdict}"
        )
        if record["snapshots_with_id"]:
            lines.append(
                f"    备份快照含该 id：{', '.join(record['snapshots_with_id'])}"
                f"（共 {len(record['snapshots_with_id'])} 个）"
            )
        else:
            lines.append("    备份快照含该 id：无")

    lines.extend(["", "【快照扫描明细】"])
    if not report["snapshots"]:
        lines.append("  （无可用快照）")
    for snap in report["snapshots"]:
        lines.append(
            f"- {snap['name']}：命中 {snap['matched_rows']} 行"
            f"（memories={snap['tables'].get('memories', 0)}，"
            f"memory_archive={snap['tables'].get('memory_archive', 0)}）；"
            f"raw 文件 {snap['raw_files']}"
        )

    lines.extend(
        [
            "",
            "【原始层提醒】",
            f"  raw/ 共 {report['raw_layer']['files']} 个文件。"
            f"{report['raw_layer']['note']}",
            "",
            "【汇总】",
            f"  待报告 id {summary['ids']} 个：活库仍有残留 {summary['ids_still_active']} 个，"
            f"备份快照仍含 {summary['ids_in_snapshots']} 个；"
            f"有命中的快照 {summary['snapshots_with_hits']} 个",
        ]
    )
    if report["warnings"]:
        lines.extend(["", "【告警】"])
        lines.extend(f"  ⚠️ {warning}" for warning in report["warnings"])
    lines.extend(["", "说明：本脚本只报告，不执行清理（快照清理见 scripts/purge_backups.py）"])
    return "\n".join(lines)


def _resolve_defaults(args) -> tuple[Path, Path]:
    """解析数据目录与备份目录（复用 sgme.config 的口径）。"""
    from sgme import config as sgme_config

    data_dir = Path(args.data_dir) if args.data_dir else Path(sgme_config.DATA_DIR)
    if args.backup_dir:
        backup_dir = Path(args.backup_dir)
    else:
        import contextlib

        # load_config() 会向 stdout 打印加载摘要——转 stderr，保证 --json 的 stdout 干净
        with contextlib.redirect_stdout(sys.stderr):
            cfg = sgme_config.load_config()
        backup_dir = Path(sgme_config.resolve_backup_dir(cfg))
    return Path(data_dir), Path(backup_dir)


def _parse_memory_ids(values: list[str] | None) -> list[str] | None:
    """--memory-id 可重复，也允许逗号分隔；None 表示未显式指定。"""
    if not values:
        return None
    ids: list[str] = []
    for item in values:
        ids.extend(part.strip() for part in str(item).split(",") if part.strip())
    if not ids:
        raise ValueError("--memory-id 不能为空；可重复传或逗号分隔")
    return ids


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(
        description="已彻底删除记忆的剩余副本报告（只读）"
    )
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument(
        "--data-dir",
        default=None,
        help="活库数据目录（缺省 sgme.config.DATA_DIR；只读打开 memory.db）",
    )
    ap.add_argument(
        "--backup-dir",
        default=None,
        help="备份快照目录（缺省 sgme.config.resolve_backup_dir）",
    )
    ap.add_argument(
        "--memory-id",
        action="append",
        default=None,
        help="显式指定 id（可重复 / 逗号分隔；缺省读 deletion_audit mode='purge'）",
    )
    args = ap.parse_args(argv)

    try:
        memory_ids = _parse_memory_ids(args.memory_id)
    except ValueError as exc:
        ap.error(str(exc))

    try:
        data_dir, backup_dir = _resolve_defaults(args)
    except Exception as exc:  # noqa: BLE001 - CLI 兜底：配置异常给可读错误
        if args.json:
            print(
                json.dumps(
                    {"read_only": True, "error": f"解析数据/备份目录失败：{exc}"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"❌ 解析数据/备份目录失败：{exc}")
        return 1

    report = build_report(data_dir, backup_dir, memory_ids=memory_ids)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
