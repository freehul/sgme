#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/export_memories_for_audit.py：把生产库 active 记忆导出成分批 jsonl，供离线体检。

输出：<out>/batches/batch-001.jsonl ... 每行 {"memory_id","content","dimensions","updated_at"}
      <out>/meta.json  统计
用法：python scripts/export_memories_for_audit.py --out audit-20261113 --batch 800
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path


def load_env():
    out = {}
    p = Path.home() / ".dsh" / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", type=int, default=800)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--sleep", type=float, default=0.45, help="每页间隔秒数（服务端 429 限流）")
    args = ap.parse_args()

    import httpx
    env = load_env()
    base = (env.get("SGME_BASE_URL") or ("http://" + env.get("SGME_HTTP_HOST", "127.0.0.1") + ":9910")).rstrip("/")
    key = env.get("SGME_ADMIN_KEY")
    out = Path(args.out); (out / "batches").mkdir(parents=True, exist_ok=True)

    items, page = [], 1
    with httpx.Client(timeout=120, trust_env=False, headers={"X-API-Key": key}) as c:
        while True:
            for attempt in range(8):
                r = c.get(base + "/v1/admin/memories",
                          params={"page": page, "limit": args.limit, "status": "active"})
                if r.status_code != 429:
                    break
                __import__("time").sleep(3.0 * (attempt + 1))
            r.raise_for_status()
            __import__("time").sleep(args.sleep)
            j = r.json()
            batch = j.get("items") or []
            items.extend(batch)
            total = j.get("total") or 0
            if not batch or len(items) >= total:
                break
            page += 1
            if page > 500:
                break
    rows = [{"memory_id": it.get("memory_id"), "content": it.get("content", ""),
             "dimensions": it.get("dimensions") or it.get("dimension_ids") or [],
             "updated_at": it.get("updated_at")} for it in items if it.get("memory_id")]

    n = 0
    for i in range(0, len(rows), args.batch):
        n += 1
        p = out / "batches" / ("batch-%03d.jsonl" % n)
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows[i:i + args.batch]) + "\n", encoding="utf-8")
    (out / "meta.json").write_text(json.dumps({"total": len(rows), "batches": n, "batch_size": args.batch,
                                               "exported_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("导出 %d 条 → %d 个批次（每批 %d）→ %s" % (len(rows), n, args.batch, out))


if __name__ == "__main__":
    main()
