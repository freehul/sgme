#!/usr/bin/env python3
"""near_dup_bulk.py — 近似重复隔离区批量丢弃（2026-11-08）。

用户拍板：相似度 ≥0.92 无需人工复核，可直接丢弃。本工具调用
`POST /v1/admin/near-dup/bulk-discard`（一次请求在服务端批量 UPDATE），
默认 dry-run；`--apply` 才真正写。行保留 status='discarded' + discard_reason，
可人工 restore 翻案。

用法：
  SGME_ADMIN_KEY=... python scripts/near_dup_bulk.py --base-url <url>            # dry-run
  SGME_ADMIN_KEY=... python scripts/near_dup_bulk.py --base-url <url> --apply    # 真跑
  ... --min-score 0.92 --reason "auto_backlog:score>=0.92"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _request(method: str, url: str, api_key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", api_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def bulk_endpoint(base_url: str, api_key: str, *, min_score: float, reason: str, apply: bool) -> dict:
    body = {"min_score": min_score, "reason": reason, "apply": apply}
    return _request("POST", f"{base_url}/v1/admin/near-dup/bulk-discard", api_key, body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="近似重复隔离区批量丢弃（默认 dry-run）")
    ap.add_argument("--base-url", default=os.environ.get("SGME_HTTP_URL", "http://127.0.0.1:9910"))
    ap.add_argument("--api-key-env", default="SGME_ADMIN_KEY")
    ap.add_argument("--min-score", type=float, default=0.92)
    ap.add_argument("--reason", default="auto_backlog:score>=0.92")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只 dry-run）")
    args = ap.parse_args(argv)

    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        print(f"缺少 {args.api_key_env}（请显式导出后再运行）", file=sys.stderr)
        return 2
    base = args.base_url.rstrip("/")

    try:
        dry = bulk_endpoint(base, api_key, min_score=args.min_score, reason=args.reason, apply=False)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        print(f"预检失败：{e}", file=sys.stderr)
        return 1
    print(f"命中={dry.get('matched')}  min_score={args.min_score:.2f}  apply={args.apply}")
    if not args.apply:
        print("dry-run 结束；确认后加 --apply。")
        return 0

    try:
        res = bulk_endpoint(base, api_key, min_score=args.min_score, reason=args.reason, apply=True)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        print(f"执行失败：{e}", file=sys.stderr)
        return 1
    print(f"完成：matched={res.get('matched')} discarded={res.get('discarded')} reason={res.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
