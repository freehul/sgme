# -*- coding: utf-8 -*-
"""sgme/scene_scan.py：9.6 场景层下线**只读**依赖扫描（2026-11-04）。

用途：下线前先回答三个问题，不删任何东西、不改任何库——
1. 库里场景数据还有多少（scenes / scene_vectors / scenes_fts / 场景边）；
2. 代码里谁还在消费场景端点与场景表（按文件计数，区分 server 内部 vs
   adapters/WebUI 外部消费方）；
3. 下线清单（端点 410 / 只读保留 / 四表冻结 / 代码摘除顺序）。

设计依据：9.6「四表冻结 + POST scenes/{id}/status 与 scene-gc/trigger 下线，
GET /v1/admin/scenes 保留只读；摘除后 e2e_smoke + 9.4 #9/#10 必须全绿」。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".vue"}
SCAN_DIRS = ("sgme", "adapters", "scripts", "ui/src")
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "dist", "tmp", ".venv"}
ENDPOINT_PATTERNS = ("/v1/admin/scenes", "/v1/admin/scene-gc")
TABLE_PATTERNS = ("scene_vectors", "scenes_fts", "scenes", "scene_gc", "scene_key")


def scan_code(root: Path | str = ROOT) -> dict:
    """只读扫描代码里的场景消费点（按文件聚合计次）。"""
    root = Path(root)
    files: list[dict] = []
    for rel in SCAN_DIRS:
        base = root / rel
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in CODE_EXTS:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hits = {pat: text.count(pat) for pat in ENDPOINT_PATTERNS if pat in text}
            hits.update({pat: text.count(pat) for pat in TABLE_PATTERNS if pat in text})
            if not hits:
                continue
            relpath = str(path.relative_to(root)).replace("\\", "/")
            files.append({
                "path": relpath,
                "hits": hits,
                "consumer": relpath.startswith("adapters/") or relpath.startswith("ui/src/"),
            })
    files.sort(key=lambda f: sum(f["hits"].values()), reverse=True)
    return {
        "files_scanned": len(files),
        "endpoint_hits": sum(
            n for f in files for p, n in f["hits"].items() if p in ENDPOINT_PATTERNS
        ),
        "consumer_files": [f["path"] for f in files if f["consumer"]],
        "top_files": files[:20],
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def scan_db(data_dir: Path | str) -> dict:
    """只读（mode=ro）统计场景数据量；库不存在返回 available=False。"""
    db_path = Path(data_dir) / "memory.db"
    if not db_path.exists():
        return {"available": False, "path": str(db_path)}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out: dict = {"available": True, "path": str(db_path), "tables": {}}
        for table in ("scenes", "scene_vectors", "scenes_fts", "memory_edges",
                      "memory_pages"):
            if not _has_table(conn, table):
                out["tables"][table] = {"exists": False}
                continue
            count = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            out["tables"][table] = {"exists": True, "rows": count}
        if _has_table(conn, "memory_edges"):
            rows = conn.execute(
                "SELECT source, COUNT(*) c FROM memory_edges GROUP BY source"
            ).fetchall()
            out["edge_sources"] = {r["source"]: r["c"] for r in rows}
        if _has_table(conn, "memory_pages"):
            rows = conn.execute(
                "SELECT kind, COUNT(*) c FROM memory_pages GROUP BY kind"
            ).fetchall()
            out["page_kinds"] = {r["kind"]: r["c"] for r in rows}
        return out
    finally:
        conn.close()


CHECKLIST = [
    {
        "item": "端点 410",
        "detail": "POST /v1/admin/scenes/{id}/status、POST /v1/admin/scene-gc/trigger",
        "action": "D-64 已登记 GONE；9.6 实装后打开 compat.enforce_gone 或直接移除路由",
        "owner": "server",
    },
    {
        "item": "只读保留",
        "detail": "GET /v1/admin/scenes（WebUI 仍看冻结数据）",
        "action": "保留，不删列表端点",
        "owner": "server",
    },
    {
        "item": "四表冻结",
        "detail": "scenes / scene_vectors / scenes_fts / 场景边",
        "action": "生产置只读 + deprecated_2_0 标记（库里打标，不删行）",
        "owner": "prod",
    },
    {
        "item": "代码摘除",
        "detail": "按 top_files 顺序摘 scene 分支（最重：engine/l2.py、data/search、routes_admin）",
        "action": "摘除后跑 scripts/e2e_smoke.py + 9.4 #9/#10 全绿",
        "owner": "dev",
    },
    {
        "item": "兼容期口径",
        "detail": "带 scenes scope 的检索结果会变（9.4 #10）",
        "action": "出「新增/消失 top 50」清单 + 人工抽查 20 条",
        "owner": "dev",
    },
]


def build_report(root: Path | str = ROOT, data_dir: Path | str | None = None) -> dict:
    """汇总扫描报告（不写库；调用方决定是否落盘）。"""
    data_dir = Path(data_dir) if data_dir else ROOT / "data"
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": str(root),
        "code": scan_code(root),
        "db": scan_db(data_dir),
        "checklist": CHECKLIST,
        "note": "只读扫描：未改库、未删文件；410 开关与四表冻结属生产操作，需用户窗口确认",
    }


def write_report(report: dict, out: Path | str) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
