# -*- coding: utf-8 -*-
"""scripts/purge_backups.py：备份快照里清除「已彻底删除」的内容（D-44 ③，默认 dry-run）。

背景：D-44 彻底删除只清活库；备份快照（最长 30 天）仍含已删内容。本脚本按
活库 deletion_audit（mode='purge'）里的 memory_id 列表，重写各快照的 memory.db，
把对应行从 memories / memory_archive / 子表 / 边里清掉；FTS 由触发器同步。

安全：
- 默认 dry-run（只统计）；--apply 才写；
- 必须显式 --confirm PURGE-BACKUPS（第二把钥匙），否则拒绝执行；
- 每个快照先写临时文件再原子替换；失败保留原文件；
- 审计：写活库 deletion_audit（memory_id='__backup_purge__', mode='purge',
  reason 记快照清单）+ 报告 JSON。
- 不处理 raw/：L0 原件段落 redact 仍是人工步骤（脚本只报告 raw 文件数）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIRM_PHRASE = "PURGE-BACKUPS"
SENTINEL = "__backup_purge__"
CHILD_TABLES = ("memory_vectors", "memory_stats", "memory_tags", "memory_sources",
                "memory_pages")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def purged_ids(data_dir: Path) -> list[str]:
    """活库 deletion_audit 里彻底删除的 memory_id（排除本脚本的审计哨兵）。"""
    db_path = Path(data_dir) / "memory.db"
    if not db_path.exists():
        return []
    conn = _ro(db_path)
    try:
        if not _has_table(conn, "deletion_audit"):
            return []
        rows = conn.execute(
            "SELECT DISTINCT memory_id FROM deletion_audit WHERE mode='purge'"
        ).fetchall()
        # 显式排除本脚本的审计哨兵（SQL LIKE 的 "_" 是通配符，不能写 '__%'）
        return [r["memory_id"] for r in rows
                if not str(r["memory_id"]).startswith("__")]
    finally:
        conn.close()


def scan_snapshot_db(db_path: Path, ids: list[str]) -> dict:
    """只读统计快照里命中的行数（dry-run）。"""
    if not db_path.exists():
        return {"available": False, "matched": 0, "tables": {}}
    conn = _ro(db_path)
    try:
        out: dict = {"available": True, "matched": 0, "tables": {}}
        if not ids:
            return out
        marks = ",".join("?" * len(ids))
        for table in ("memories", "memory_archive"):
            if not _has_table(conn, table):
                continue
            n = conn.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE memory_id IN ({marks})",
                tuple(ids),
            ).fetchone()["c"]
            out["tables"][table] = n
            out["matched"] += n
        return out
    finally:
        conn.close()


def purge_snapshot_db(db_path: Path, ids: list[str]) -> dict:
    """重写一个快照 memory.db：清除命中行（临时文件 + 原子替换）。"""
    if not ids or not db_path.exists():
        return {"removed": 0, "tables": {}}
    tmp = db_path.with_suffix(".purging")
    if tmp.exists():
        tmp.unlink()
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        src.close()
        dst.row_factory = sqlite3.Row
        marks = ",".join("?" * len(ids))
        counts: dict[str, int] = {}
        dst.execute("BEGIN")
        for table in CHILD_TABLES:
            if _has_table(dst, table):
                cur = dst.execute(
                    f"DELETE FROM {table} WHERE memory_id IN ({marks})", tuple(ids)
                )
                counts[table] = cur.rowcount
        if _has_table(dst, "memory_edges"):
            cur = dst.execute(
                f"DELETE FROM memory_edges WHERE from_id IN ({marks}) "
                f"OR to_id IN ({marks})", tuple(ids) + tuple(ids)
            )
            counts["memory_edges"] = cur.rowcount
        for table in ("memory_archive", "memories"):
            if _has_table(dst, table):
                cur = dst.execute(
                    f"DELETE FROM {table} WHERE memory_id IN ({marks})", tuple(ids)
                )
                counts[table] = cur.rowcount
        dst.commit()
        dst.close()
        os.replace(tmp, db_path)
        removed = sum(v for k, v in counts.items() if k in ("memories", "memory_archive"))
        return {"removed": removed, "tables": counts}
    except Exception:
        try:
            src.close()
        except sqlite3.Error:
            pass
        try:
            dst.close()
        except sqlite3.Error:
            pass
        if tmp.exists():
            tmp.unlink()
        raise


def run(data_dir: Path, backup_dir: Path, *, apply: bool = False) -> dict:
    """扫描/清理全部快照；返回报告（不写审计）。"""
    ids = purged_ids(data_dir)
    snaps = []
    if backup_dir.exists():
        for d in sorted(backup_dir.iterdir()):
            db = d / "memory.db"
            if not d.is_dir() or not db.exists():
                continue
            scan = scan_snapshot_db(db, ids)
            item = {"snapshot": d.name, "matched": scan["matched"],
                    "tables": scan["tables"], "applied": False}
            if apply and scan["matched"]:
                item.update(purge_snapshot_db(db, ids))
                item["applied"] = True
            raw_dir = d / "raw"
            item["raw_files"] = sum(1 for _ in raw_dir.rglob("*")) if raw_dir.exists() else 0
            snaps.append(item)
    return {
        "generated_at": _now_iso(),
        "apply": apply,
        "purged_ids": len(ids),
        "snapshots": snaps,
        "total_matched": sum(s["matched"] for s in snaps),
    }


def _audit(data_dir: Path, report: dict) -> None:
    db_path = Path(data_dir) / "memory.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO deletion_audit "
            "(memory_id, mode, triggered_by, agent_id, reason, content_sha256, "
            " snapshot_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                SENTINEL, "purge", "user", "script",
                "backup_purge:" + ",".join(s["snapshot"] for s in report["snapshots"] if s["applied"]),
                None,
                json.dumps({"snapshots": [s["snapshot"] for s in report["snapshots"] if s["applied"]],
                            "matched": report["total_matched"]}, ensure_ascii=False),
                _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="备份快照清除已彻底删除内容（默认 dry-run）")
    ap.add_argument("--data-dir", default=None, help="活库数据目录（读 deletion_audit）")
    ap.add_argument("--backup-dir", default=None, help="快照目录（缺省 config.resolve_backup_dir）")
    ap.add_argument("--apply", action="store_true", help="真正重写快照（默认只统计）")
    ap.add_argument("--confirm", default="", help="第二把钥匙：必须为 PURGE-BACKUPS")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None, help="报告路径（缺省 docs/migration/...json）")
    args = ap.parse_args(argv)

    if args.apply and args.confirm != CONFIRM_PHRASE:
        print(f"[拒绝] --apply 需同时传 --confirm {CONFIRM_PHRASE}", file=sys.stderr)
        return 2

    from sgme import config as sgme_config

    data_dir = Path(args.data_dir) if args.data_dir else Path(sgme_config.DATA_DIR)
    backup_dir = (Path(args.backup_dir) if args.backup_dir
                  else Path(sgme_config.resolve_backup_dir(sgme_config.load_config())))

    report = run(data_dir, backup_dir, apply=args.apply)
    stamp = datetime.now().strftime("%Y%m%d")
    out = Path(args.out) if args.out else (
        ROOT / "docs" / "migration" / f"2.0-backup-purge-{stamp}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.apply:
        _audit(data_dir, report)

    if args.json:
        print(json.dumps({"report": str(out), "apply": report["apply"],
                          "purged_ids": report["purged_ids"],
                          "total_matched": report["total_matched"]},
                         ensure_ascii=False, indent=2))
    else:
        print(f"备份 purge（{'APPLY' if report['apply'] else 'DRY-RUN'}）："
              f"活库已彻底删除 id {report['purged_ids']} 个；命中快照行 {report['total_matched']}")
        for s in report["snapshots"]:
            print(f"- {s['snapshot']}: 命中 {s['matched']}；raw 文件 {s['raw_files']}"
                  + ("；已重写" if s["applied"] else ""))
        print(f"- 报告：{out}")
        print("- 注意：raw/ 原件段落 redact 仍需人工（本脚本不处理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
