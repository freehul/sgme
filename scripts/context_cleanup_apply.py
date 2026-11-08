#!/usr/bin/env python3
"""context_cleanup_apply.py — 存量重复清理（默认 dry-run；--apply 才归档）。

输入 plan（W11/W12 dry-run 生成）：
{
  "groups": [
    {"source": "whole_db|button_vector", "keep": "<memory_id>", "archive": ["<id>", ...]}
  ]
}

安全：
- 默认 dry-run，不写库；
- --apply 前自动用 sqlite3 backup 生成同目录 .bak-<ts>-cleanup 备份；
- 只对 status='active' 的 archive 候选执行 memory_dao.archive_memory（复制到 archive + 删原行，可溯源）；
- keeper 必须 active；keeper 自身永不归档；重复/已归档跳过；
- 不删除原件（memory_archive 保留）。
"""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_plan(args) -> dict:
    if args.plan_b64:
        raw = base64.b64decode(args.plan_b64).decode("utf-8")
    else:
        raw = Path(args.plan).read_text(encoding="utf-8")
    plan = json.loads(raw)
    if not isinstance(plan.get("groups"), list):
        raise SystemExit("plan.groups 缺失/非法")
    return plan


def _backup(db: Path) -> Path:
    dst = db.with_name(db.name + f".bak-{_now()}-cleanup")
    src = sqlite3.connect(str(db))
    out = sqlite3.connect(str(dst))
    src.backup(out)
    out.close(); src.close()
    return dst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/data/data/memory.db")
    ap.add_argument("--plan", default="")
    ap.add_argument("--plan-b64", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not args.plan_b64 and not args.plan:
        raise SystemExit("必须给 --plan 或 --plan-b64")

    db = Path(args.db)
    if not db.is_file():
        raise SystemExit(f"DB 不存在: {db}")
    plan = _load_plan(args)
    if args.apply:
        bak = _backup(db)
        print(f"BACKUP {bak} {bak.stat().st_size}", file=sys.stderr)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        from sgme.data import memory_dao
    except Exception as e:
        raise SystemExit(f"无法 import sgme: {e}")

    stats = {"groups": 0, "keeper_missing": 0, "archive_total": 0,
             "archived": 0, "already_archived": 0, "missing": 0, "errors": 0}
    for gi, group in enumerate(plan["groups"], 1):
        keep = str(group.get("keep") or "")
        if not keep:
            stats["keeper_missing"] += 1
            continue
        if not conn.execute("SELECT 1 FROM memories WHERE memory_id=? AND status='active'", (keep,)).fetchone():
            stats["keeper_missing"] += 1
            continue
        stats["groups"] += 1
        for aid in group.get("archive") or []:
            aid = str(aid)
            if aid == keep:
                continue
            stats["archive_total"] += 1
            row = conn.execute("SELECT 1 FROM memories WHERE memory_id=? AND status='active'", (aid,)).fetchone()
            if not row:
                # 区分不存在 / 已归档
                anyrow = conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (aid,)).fetchone()
                stats["already_archived" if anyrow else "missing"] += 1
                continue
            if not args.apply:
                continue
            try:
                ok = memory_dao.archive_memory(conn, aid, superseded_by=keep)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                print(f"ERR {aid}: {e}", file=sys.stderr)
                continue
            if ok:
                stats["archived"] += 1
            else:
                stats["already_archived"] += 1
    conn.close()
    stats["dry_run"] = not args.apply
    print(json.dumps(stats, ensure_ascii=False))
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
