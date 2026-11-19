# -*- coding: utf-8 -*-
"""分段同步单个超大 WorkBuddy 会话到 SGME L0（绕开网关 HTTP 413）。

背景：sync_sessions.py 把整场会话渲染成一次 append，单会话消息数极大时
（实测 7564 条）请求体超过网关 body 限制 → HTTP 413。本工具复用同一套
parse_jsonl/render_l0，按字节切批分段 append；每段用该批首条消息时间作
started_at，服务端按 session_key+started_at 幂等，重跑不重复。

用法::

    python sync_large_session.py <session-id-or-prefix> [--batch-bytes 400000]
    python sync_large_session.py e5af835a --dry-run

成功完成后更新 sync_sessions.py 的游标（sgme-sync-state.json），标记为
segmented，后续增量同步从完整消息数继续，不重发。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sgme_client import SGME  # noqa: E402
import sync_sessions as ss  # noqa: E402


def find_session(prefix: str) -> dict:
    con = sqlite3.connect(str(ss.default_db_path()))
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id,cwd,title,updated_at,deleted_at,is_playground,"
            "is_background_automation FROM sessions"
        )]
    finally:
        con.close()
    hits = [r for r in rows if str(r["id"]).lower().startswith(prefix.lower())]
    if not hits:
        raise SystemExit(f"SESSION_NOT_FOUND: {prefix}")
    if len(hits) > 1:
        raise SystemExit(f"AMBIGUOUS_PREFIX: {prefix} -> {[r['id'] for r in hits]}")
    return hits[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="分段同步超大 WorkBuddy 会话到 SGME L0")
    ap.add_argument("session", help="完整 session UUID 或前 8 位前缀")
    ap.add_argument("--batch-bytes", type=int, default=400_000,
                    help="每批正文字节上限（默认 400000，保守避开 413）")
    ap.add_argument("--dry-run", action="store_true", help="只报告切批，不写入")
    args = ap.parse_args()

    row = find_session(args.session)
    sid = str(row["id"])
    key = f"workbuddy-{sid[:8]}"
    if str(row.get("deleted_at") or "").strip() not in ("", "None", "null", "0"):
        raise SystemExit(f"SESSION_DELETED: {sid}")

    path = ss.find_jsonl(ss.default_projects_dir(), sid, str(row.get("cwd") or ""))
    if not path:
        raise SystemExit(f"JSONL_NOT_FOUND: {sid}")
    msgs = ss.parse_jsonl(path)
    if not msgs:
        raise SystemExit(f"EMPTY: {sid}")

    # 按渲染后字节数切批
    batches, cur, cur_bytes = [], [], 0
    for m in msgs:
        b = len(ss.render_l0([m]).encode("utf-8"))
        if cur and cur_bytes + b > args.batch_bytes:
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(m)
        cur_bytes += b
    if cur:
        batches.append(cur)

    print(f"session={key} messages={len(msgs)} batches={len(batches)} "
          f"batch_msg_counts={[len(b) for b in batches]}")
    if args.dry_run:
        return 0

    client = SGME()
    meta = ss.session_metadata(row)
    ok = fail = 0
    first_fid, results = None, []
    for i, batch in enumerate(batches):
        text = ss.render_l0(batch)
        started_at = batch[0]["ts"]
        try:
            resp = client.append(key, text, started_at=started_at,
                                 metadata=meta if i == 0 else None, raw=True)
            fid = resp.get("file_id") if isinstance(resp, dict) else None
            if first_fid is None:
                first_fid = fid
            ok += 1
            results.append({"batch": i, "status": "ok", "msgs": len(batch),
                            "bytes": len(text.encode()), "started_at": started_at,
                            "file_id": fid})
            print(f"  batch {i+1}/{len(batches)}: OK {len(batch)} msgs")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            results.append({"batch": i, "status": "failed", "msgs": len(batch),
                            "error": f"{type(exc).__name__}: {exc}"})
            print(f"  batch {i+1}/{len(batches)}: FAIL {type(exc).__name__}: {str(exc)[:120]}")

    if fail == 0:
        sp = ss.default_state_path()
        state = ss.load_state(sp)
        state["sessions"][sid] = {
            "updated_at": str(row.get("updated_at") or ""),
            "file_id": first_fid,
            "synced_messages": len(msgs),
            "session_key": key,
            "segmented": True,
            "segments": len(batches),
        }
        ss.save_state(sp, state)
        print("STATE_UPDATED")
    else:
        print("STATE_NOT_UPDATED (has failures; safe to rerun — started_at is idempotent)")
    print(f"DONE ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
