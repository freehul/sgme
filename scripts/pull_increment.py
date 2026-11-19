#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/pull_increment.py：把生产库「某日期之后」的 L0 会话增量补进隔离重建 home。

原理（读生产、写隔离库，生产零改动）：
  GET /v1/admin/sessions?since=<date>   列出会话元数据（分页，limit<=200）
  GET /v1/admin/sessions/{file_id}      取回会话原文全文
  → 写 <home>/raw/sessions/<file_id>.md + 插 session.db raw_files(status='new')

幂等：home 里已存在的 file_id 一律跳过（按 file_id 判定，不按日期）。

用法：
    python scripts/pull_increment.py --home rebuild-20261109/home --since 2026-11-05 --dry-run
    python scripts/pull_increment.py --home rebuild-20261109/home --since 2026-11-05
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    p = Path.home() / ".dsh" / ".env"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="拉取生产 L0 增量到隔离重建 home")
    ap.add_argument("--home", required=True)
    ap.add_argument("--since", default="2026-11-05", help="起始日期（YYYY-MM-DD 或 ISO）")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--limit", type=int, default=200, help="分页大小（服务端上限 200）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)

    import httpx

    env = _load_env_file()
    base = (args.base_url or env.get("SGME_BASE_URL")
            or ("http://" + env.get("SGME_HTTP_HOST", "127.0.0.1") + ":9910")).rstrip("/")
    key = env.get("SGME_ADMIN_KEY") or os.environ.get("SGME_ADMIN_KEY")
    if not key:
        print("缺少 SGME_ADMIN_KEY（环境变量或 ~/.dsh/.env）")
        return 2

    home = Path(args.home).resolve()
    db_path = home / "data" / "session.db"
    raw_dir = home / "raw" / "sessions"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        print("找不到 session.db: " + str(db_path))
        return 2

    client = httpx.Client(timeout=args.timeout, trust_env=False,
                          headers={"X-API-Key": key})

    # 1) 列增量（分页）
    items: list[dict] = []
    page = 1
    while True:
        r = client.get(base + "/v1/admin/sessions",
                       params={"since": args.since, "limit": args.limit, "page": page})
        r.raise_for_status()
        j = r.json()
        batch = j.get("items") or []
        items.extend(batch)
        total = j.get("total") or 0
        if len(batch) < args.limit or len(items) >= total:
            break
        page += 1
        if page > 50:
            print("分页超过 50 页，中止（检查 since/limit）")
            return 2

    conn = sqlite3.connect(db_path)
    existing = {row[0] for row in conn.execute("select file_id from raw_files")}
    todo = [it for it in items if it.get("file_id") not in existing]
    print("生产增量会话: %d 条 | home 已有: %d | 需补: %d" % (len(items), len(existing), len(todo)))
    for it in todo:
        print("  %s  %s  %s  %sB" % (it.get("file_id", "")[:8], it.get("started_at", "")[:19],
                                     it.get("session_key", ""), it.get("size")))

    if args.dry_run or not todo:
        conn.close()
        return 0

    # 2) 逐条拉原文并落盘 + 建索引
    added = 0
    for it in todo:
        fid = it["file_id"]
        r = client.get(base + "/v1/admin/sessions/" + fid)
        if r.status_code != 200:
            print("  取原文失败 %s -> HTTP %s" % (fid[:8], r.status_code))
            continue
        body = r.json()
        content = body.get("content") or ""
        if not content.strip():
            print("  跳过空原文 %s" % fid[:8])
            continue
        (raw_dir / (fid + ".md")).write_text(content, encoding="utf-8")
        conn.execute(
            """insert into raw_files
               (file_id, path, session_key, agent_id, started_at, ended_at, refined_at,
                last_refined_seq, status, size, content_hash, agent_model, fail_count, last_error)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, "raw/sessions/%s.md" % fid, body.get("session_key") or it.get("session_key"),
             body.get("agent_id") or it.get("agent_id"), it.get("started_at"), it.get("ended_at"),
             None, 0, "new", len(content.encode("utf-8")),
             hashlib.sha256(content.encode("utf-8")).hexdigest(), None, 0, None),
        )
        added += 1
    conn.commit()
    conn.close()
    print("已补入 %d 条（status=new）" % added)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
