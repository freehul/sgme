# -*- coding: utf-8 -*-
"""scripts/scene_search_baseline.py：场景检索路移除前后 top50 对照（只读，9.4 #10）。

用途（T-427 / A-7 S3 第 0 步）：
1. 改代码前用当前版本跑固定 query 集，落 ``scopes=["wiki"]`` 与
   ``scopes=["wiki_pages"]`` 两口径的 top50 快照；
2. 改完后再跑一次，输出「新增/消失 top 50」差异清单；
3. 人工抽查 20 条（脚本给出抽样清单，核对由人完成）。

两种取数方式（任选）：
- ``--api-base``：打线上 Gateway ``POST /v1/search``（与 HEAD 同代码时即为 before 基线；
  API Key 只从环境变量读，不落盘、不打印）；
- ``--data-dir``：本进程内直连本地三库调 ``sgme.operations.search.search``（离线复跑）。

只读：不写数据库，只写 ``--out`` 指定的 JSON 快照/差异报告（默认 tmp/）。

用法：
  # before 快照（线上）
  python scripts/scene_search_baseline.py --api-base http://<NAS_IP>:9910 \
      --api-key-env SGME_ADMIN_KEY --out tmp/scene_search_before.json

  # after 快照（部署后同命令，换 --out）
  python scripts/scene_search_baseline.py --api-base http://<NAS_IP>:9910 \
      --api-key-env SGME_ADMIN_KEY --out tmp/scene_search_after.json

  # 差异
  python scripts/scene_search_baseline.py --diff tmp/scene_search_before.json \
      tmp/scene_search_after.json --out tmp/scene_search_diff.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

#: 固定 query 集（10 条，覆盖用户/项目/技术/家庭/运维等主要叙事主题；
#: 不随数据集变化，保证 before/after 可逐条对照）
QUERIES: tuple[str, ...] = (
    "SGME 记忆引擎",
    "生产环境部署与运维",
    "用户技术原则与协作铁律",
    "家庭情况与透析安排",
    "VPS 采购与安全加固",
    "Hermes 技能系统管理",
    "本地 AI 运行环境与模型选型",
    "DSH 接入与 MCP 连接",
    "AIRDT 弱电项目",
    "视频下载与分析流水线",
)

#: 两口径：wiki（历史 = 场景层 + 页面层）、wiki_pages（仅页面层）
SCOPES: tuple[tuple[str, list[str]], ...] = (
    ("wiki", ["wiki"]),
    ("wiki_pages", ["wiki_pages"]),
)

TOP_N = 50


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _api_search(base: str, key: str, query: str, scopes: list[str]) -> list[dict]:
    """调线上 /v1/search（trust_env=False，防代理劫持 localhost）。"""
    import requests

    s = requests.Session()
    s.trust_env = False
    resp = s.post(
        f"{base.rstrip('/')}/v1/search",
        json={"query": query, "scopes": scopes, "limit": TOP_N},
        headers={"X-API-Key": key},
        timeout=120,
    )
    resp.raise_for_status()
    return list(resp.json().get("results") or [])


def _local_search(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    wiki_conn: sqlite3.Connection | None,
    cfg: dict,
    query: str,
    scopes: list[str],
) -> list[dict]:
    """进程内调 operations.search（离线复跑，不依赖线上服务）。"""
    from sgme.operations.search import search as search_op

    result = search_op(
        mem_conn,
        session_conn,
        cfg,
        query=query,
        scopes=scopes,
        limit=TOP_N,
        wiki_conn=wiki_conn,
    )
    if not result.ok:
        raise RuntimeError(f"检索失败: {result.error_code} {result.message}")
    return list((result.data or {}).get("results") or [])


def _item_key(item: dict) -> str:
    """稳定身份键：source + 该层主 id（不用 rank，避免排序抖动误判）。"""
    ident = (
        item.get("scene_id")
        or item.get("page_id")
        or item.get("memory_id")
        or item.get("file_id")
        or ""
    )
    return f"{item.get('source', '')}:{ident}"


def _trim(item: dict, rank: int) -> dict:
    content = (item.get("content") or "").replace("\r", "")
    return {
        "rank": rank,
        "key": _item_key(item),
        "source": item.get("source"),
        "id": item.get("scene_id") or item.get("page_id") or item.get("memory_id") or "",
        "title": item.get("title") or "",
        "content_prefix": content[:160],
        "routes": item.get("routes") or [],
    }


def _snapshot_api(base: str, key: str) -> dict:
    return {
        "generated_at": _now(),
        "mode": "api",
        "api_base": base,
        "top_n": TOP_N,
        "queries": [
            {
                "query": q,
                "scope": scope_name,
                "scopes": scopes,
                "results": [
                    _trim(it, i + 1)
                    for i, it in enumerate(_api_search(base, key, q, scopes)[:TOP_N])
                ],
            }
            for q in QUERIES
            for scope_name, scopes in SCOPES
        ],
    }


def _snapshot_local(data_dir: Path) -> dict:
    from sgme import config as config_mod
    from sgme.data import db as db_mod

    cfg = config_mod.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(data_dir)
    try:
        return {
            "generated_at": _now(),
            "mode": "local",
            "data_dir": str(data_dir),
            "top_n": TOP_N,
            "queries": [
                {
                    "query": q,
                    "scope": scope_name,
                    "scopes": scopes,
                    "results": [
                        _trim(it, i + 1)
                        for i, it in enumerate(
                            _local_search(
                                mem_conn, session_conn, wiki_conn, cfg, q, scopes
                            )[:TOP_N]
                        )
                    ],
                }
                for q in QUERIES
                for scope_name, scopes in SCOPES
            ],
        }
    finally:
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        if wiki_conn is not None:
            db_mod.close(wiki_conn)


def _diff(before: dict, after: dict) -> dict:
    """按 (query, scope) 比对身份键：新增 / 消失（各保留 top50 顺序）。"""
    before_map = {(x["query"], x["scope"]): x for x in before.get("queries") or []}
    after_map = {(x["query"], x["scope"]): x for x in after.get("queries") or []}
    rows: list[dict] = []
    total_added = total_removed = 0
    for key in sorted(set(before_map) | set(after_map)):
        b = before_map.get(key) or {"results": []}
        a = after_map.get(key) or {"results": []}
        b_keys = [r["key"] for r in b.get("results") or []]
        a_keys = [r["key"] for r in a.get("results") or []]
        added = [r for r in a.get("results") or [] if r["key"] not in set(b_keys)][:TOP_N]
        removed = [r for r in b.get("results") or [] if r["key"] not in set(a_keys)][:TOP_N]
        total_added += len(added)
        total_removed += len(removed)
        rows.append({
            "query": key[0],
            "scope": key[1],
            "before_count": len(b_keys),
            "after_count": len(a_keys),
            "added": added,
            "removed": removed,
        })
    return {
        "generated_at": _now(),
        "before_generated_at": before.get("generated_at"),
        "after_generated_at": after.get("generated_at"),
        "top_n": TOP_N,
        "summary": {
            "pairs": len(rows),
            "added": total_added,
            "removed": total_removed,
        },
        "rows": rows,
        # 人工抽查清单：按消失条数排序，取前 20 条（含正文前缀，供逐条确认无关键信息丢失）
        "manual_sample": [
            r for row in sorted(rows, key=lambda x: -len(x["removed"]))
            for r in row["removed"][:3]
        ][:20],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="场景检索路前后 top50 对照（只读）")
    ap.add_argument("--api-base", default=None, help="线上 Gateway 地址（如 http://<NAS_IP>:9910）")
    ap.add_argument("--api-key-env", default="SGME_ADMIN_KEY", help="API Key 所在环境变量名")
    ap.add_argument("--data-dir", default=None, help="本地数据目录（离线直连三库）")
    ap.add_argument("--out", default=None, help="输出 JSON（默认 tmp/scene_search_<mode>.json）")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"), default=None,
                    help="比对两个快照，输出新增/消失清单")
    args = ap.parse_args(argv)

    if args.diff:
        before = json.loads(Path(args.diff[0]).read_text(encoding="utf-8"))
        after = json.loads(Path(args.diff[1]).read_text(encoding="utf-8"))
        report = _diff(before, after)
        out = Path(args.out) if args.out else ROOT / "tmp" / "scene_search_diff.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        s = report["summary"]
        print(f"差异：{s['pairs']} 组 query/scope，新增 {s['added']} / 消失 {s['removed']}")
        for row in report["rows"]:
            print(f"- [{row['scope']}] {row['query']}: "
                  f"{row['before_count']} → {row['after_count']}，"
                  f"新增 {len(row['added'])} / 消失 {len(row['removed'])}")
        print(f"报告：{out}")
        return 0

    if args.api_base:
        key = os.environ.get(args.api_key_env or "")
        if not key:
            print(f"缺少环境变量 {args.api_key_env}", file=sys.stderr)
            return 2
        snap = _snapshot_api(args.api_base, key)
        default_name = "scene_search_snapshot.json"
    elif args.data_dir:
        snap = _snapshot_local(Path(args.data_dir))
        default_name = "scene_search_snapshot.json"
    else:
        print("必须指定 --api-base 或 --data-dir（或 --diff）", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else ROOT / "tmp" / default_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"快照（{snap['mode']}）：{len(snap['queries'])} 组 query/scope → {out}")
    for row in snap["queries"]:
        print(f"- [{row['scope']}] {row['query']}: {len(row['results'])} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
