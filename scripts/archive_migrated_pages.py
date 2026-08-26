# -*- coding: utf-8 -*-
"""批量把 wiki 的 skill:* 原页置 superseded（M4a 收尾：技能已全部入库）。

用法（本机运行，打 NAS 生产 API）：
    SGME_ADMIN_KEY=xxx python scripts/archive_migrated_pages.py --api http://192.168.10.10:9910

安全：只处理 title 以 skill: 开头且 status='active' 的页；带 --dry-run 预览。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests


def main() -> int:
    ap = argparse.ArgumentParser(description="M4a 收尾：wiki skill:* 原页批量置 superseded")
    ap.add_argument("--api", required=True)
    ap.add_argument("--key-env", default="SGME_ADMIN_KEY")
    ap.add_argument("--dry-run", action="store_true", help="只列出目标页，不执行 PATCH")
    args = ap.parse_args()

    api_key = os.environ.get(args.key_env, "")
    if not api_key:
        print(f"错误：环境变量 {args.key_env} 未设置", file=sys.stderr)
        return 2
    base = args.api.rstrip("/")
    headers = {"X-API-Key": api_key}
    s = requests.Session()
    s.trust_env = False  # 项目铁律：防代理劫持

    # 分页拉全量列表，筛 title 带 skill: 前缀且 active 的页
    targets = []
    offset = 0
    while True:
        r = s.get(f"{base}/v1/wiki/pages",
                  params={"limit": 200, "offset": offset}, headers=headers, timeout=30)
        if r.status_code != 0 and r.status_code != 200:
            print(f"拉取失败: HTTP {r.status_code}", file=sys.stderr)
            return 1
        items = (r.json() or {}).get("pages") or []
        if not isinstance(items, list) or not items:
            break
        for it in items:
            title = str(it.get("title") or "")
            if title.startswith("skill:") and it.get("status") == "active":
                targets.append((it.get("page_id"), title))
        if len(items) < 1:
            break
        offset += len(items)
        if offset > 10000:
            break

    print(f"目标页数: {len(targets)}")
    if args.dry_run:
        for pid, title in targets[:20]:
            print(f"  [dry-run] {pid} {title}")
        if len(targets) > 20:
            print(f"  … 其余 {len(targets)-20} 条略")
        return 0

    ok = fail = 0
    for i, (pid, title) in enumerate(targets, 1):
        attempt = 0
        while True:
            r = s.patch(
                f"{base}/v1/wiki/pages/{pid}",
                headers=headers,
                json={"content": "", "status": "superseded"},
                timeout=30,
            )
            if r.status_code == 429 and attempt < 4:
                wait_s = int(r.headers.get("Retry-After", "30")) + 2
                print(f"  ⏳ {pid}: 限流，退避 {wait_s}s", file=sys.stderr)
                time.sleep(wait_s)
                attempt += 1
                continue
            break
        if r.status_code == 200:
            ok += 1
        else:
            fail += 1
            print(f"- ❌ {pid} `{title}`: HTTP {r.status_code}: {r.text[:120]}")
        if i % 50 == 0:
            print(f"  进度 {i}/{len(targets)}（成功 {ok} / 失败 {fail}）")

    print(f"\n完成：成功 {ok} / 失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
