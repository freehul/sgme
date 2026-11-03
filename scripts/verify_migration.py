#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_migration.py：SGME 2.0 迁移校验十项（设计 §9.4，只读）。

阶段 0 用 migrate_2_0_baseline.py 采基线；阶段 3/4 用本脚本做**迁移前后比对**，
报告落 docs/migration/2.0-迁移校验-YYYYMMDD.{md,json}，作为发布门禁附件。

十项（机器可判 8 项 + 2 项需人工）：
1. 记忆行数守恒（vs 基线；迁移窗口内应为 0 差异）
2. 文本完整性：基线抽样 id 的 sha256 逐条比对（不一致必须为 0）
3. 向量一致：未嵌数不得多于基线
4. 全文索引一致：memories_fts 行数 = memories 行数
5. 归档表 + 场景四表行数不变（冻结前后）
6. 知识库：页数一致 + 九列回填 + 修订史覆盖（缺页先告警，阶段 3 门禁）+ 抽样 diff
7. 技能：数量一致 + 抽样 diff
8. scene_key：回填率 ≥99%，unknown 清单可导出（--export-unknown）
9. 注入回归十条固定查询（人工/自动化脚本执行；本脚本给口径与提示）
10. 场景路移除影响清单（人工抽查 20 条；本脚本给口径与提示）

判定：FAIL 存在 → EXIT=1；WARN 只在 --strict 下算失败；MANUAL 不计入判定。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.migrate_2_0_baseline import collect_report  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
MANUAL = "MANUAL"

SCENE_TABLES = ("scenes", "scene_memories", "scene_vectors", "scene_versions")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.Error:
        return None


def _sha256_text(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sample_diff(
    db_path: Path, table: str, id_col: str, content_col: str, baseline_items: list[dict]
) -> dict:
    """基线抽样 id 在当前库里的哈希比对（缺失/不一致分别计数）。"""
    conn = _ro(db_path)
    if conn is None:
        return {"checked": 0, "missing": len(baseline_items), "mismatch": [],
                "db": "missing"}
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if content_col not in cols:
            return {"checked": 0, "missing": len(baseline_items), "mismatch": [],
                    "db": f"no-column:{content_col}"}
        missing: list[str] = []
        mismatch: list[str] = []
        checked = 0
        for item in baseline_items:
            row = conn.execute(
                f"SELECT {content_col} FROM {table} WHERE {id_col}=?", (item["id"],)
            ).fetchone()
            if row is None:
                missing.append(item["id"])
                continue
            checked += 1
            if _sha256_text(row[0]) != item["sha256"]:
                mismatch.append(item["id"])
        return {"checked": checked, "missing": missing, "mismatch": mismatch}
    finally:
        conn.close()


def _check(no: int, name: str, status: str, detail: str) -> dict:
    return {"no": no, "name": name, "status": status, "detail": detail}


def find_latest_baseline(dirpath: Path) -> Path | None:
    files = sorted(dirpath.glob("*2.0-迁移基线-*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def run(
    baseline_path: str | Path,
    data_dir: str | Path,
    *,
    out_dir: str | Path | None = None,
    strict: bool = False,
    export_unknown: bool = False,
    stage: int = 0,
) -> dict:
    """执行十项校验，返回 report（含 checks / summary / 路径）。"""
    baseline_path = Path(baseline_path)
    data = Path(data_dir)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    samples = baseline.get("samples") or {}
    cur = collect_report(
        data,
        sample_memories=int(samples.get("memories", 500)),
        sample_wiki=int(samples.get("wiki", 20)),
        sample_skills=int(samples.get("skills", 20)),
        label=baseline.get("label"),
    )
    checks: list[dict] = []

    bmem = baseline.get("databases", {}).get("memory") or {}
    cmem = cur.get("databases", {}).get("memory") or {}

    # 1 行数守恒
    b_total = (bmem.get("memories") or {}).get("total")
    c_total = (cmem.get("memories") or {}).get("total")
    delta = (c_total or 0) - (b_total or 0)
    checks.append(_check(
        1, "记忆行数守恒",
        PASS if delta == 0 else FAIL,
        f"baseline={b_total} current={c_total} delta={delta}",
    ))

    # 2 文本完整性（只认基线抽样 id）
    mem_diff = _sample_diff(
        data / "memory.db", "memories", "memory_id", "content",
        baseline.get("hashes", {}).get("memories") or [],
    )
    status = PASS if (not mem_diff["mismatch"] and not mem_diff["missing"]) else FAIL
    checks.append(_check(
        2, "文本完整性（content sha256）", status,
        f"checked={mem_diff['checked']} mismatch={len(mem_diff['mismatch'])} "
        f"missing={len(mem_diff['missing'])}",
    ))

    # 3 向量一致（不得比基线更差）
    b_missing = (bmem.get("memory_vectors") or {}).get("missing")
    c_missing = (cmem.get("memory_vectors") or {}).get("missing")
    status = PASS if (
        c_missing is not None and b_missing is not None and c_missing <= b_missing
    ) else FAIL
    checks.append(_check(
        3, "向量一致（未嵌数不劣于基线）", status,
        f"baseline_missing={b_missing} current_missing={c_missing}",
    ))

    # 4 FTS 行数 = memories 行数
    fts_rows = cmem.get("memories_fts_rows")
    status = PASS if (fts_rows is not None and fts_rows == c_total) else FAIL
    checks.append(_check(4, "全文索引一致（FTS 行数 = 记忆行数）", status,
                         f"fts={fts_rows} memories={c_total}"))

    # 5 归档表 + 场景四表行数不变
    diffs = []
    if bmem.get("memory_archive") != cmem.get("memory_archive"):
        diffs.append(f"memory_archive {bmem.get('memory_archive')}->{cmem.get('memory_archive')}")
    b_scenes = bmem.get("scene_tables") or {}
    c_scenes = cmem.get("scene_tables") or {}
    for t in SCENE_TABLES:
        if b_scenes.get(t) != c_scenes.get(t):
            diffs.append(f"{t} {b_scenes.get(t)}->{c_scenes.get(t)}")
    checks.append(_check(5, "归档表与场景四表行数不变", PASS if not diffs else FAIL,
                         "；".join(diffs) if diffs else "五表计数与基线一致"))

    # 6 知识库
    bwiki = baseline.get("databases", {}).get("wiki") or {}
    cwiki = cur.get("databases", {}).get("wiki") or {}
    wiki_diffs = []
    if bwiki.get("wiki_pages") != cwiki.get("wiki_pages"):
        wiki_diffs.append(f"页数 {bwiki.get('wiki_pages')}->{cwiki.get('wiki_pages')}")
    wk_diff = _sample_diff(
        data / "wiki.db", "wiki_pages", "page_id", "content",
        baseline.get("hashes", {}).get("wiki") or [],
    )
    if wk_diff["mismatch"]:
        wiki_diffs.append(f"抽样 {len(wk_diff['mismatch'])} 页正文变化")
    cols_present = (cwiki.get("two_0") or {}).get("columns_present")
    cols_total = (cwiki.get("two_0") or {}).get("columns_total")
    if cols_present is not None and cols_total and cols_present < cols_total:
        wiki_diffs.append(f"九列仅 {cols_present}/{cols_total}")
    pages = cwiki.get("wiki_pages") or 0
    revisions = cwiki.get("wiki_revisions")
    rev_note = ""
    rev_missing = revisions is not None and bool(pages) and revisions < pages
    if rev_missing:
        # 设计 §9.4 #6：阶段 3 前判 WARN（给回填留窗口）；阶段 3 起必须 FAIL
        if int(stage) >= 3:
            rev_note = f"修订史缺页 {pages - revisions}（阶段 3 后为硬门禁 → FAIL）"
        else:
            rev_note = f"修订史缺页 {pages - revisions}（阶段 3 回填门禁，先告警）"
    check6_fail = bool(wiki_diffs) or (rev_missing and int(stage) >= 3)
    checks.append(_check(
        6, "知识库（页数/九列/修订史/抽样）",
        FAIL if check6_fail else (WARN if rev_note else PASS),
        "；".join(wiki_diffs + ([rev_note] if rev_note else [])) or "页数/九列/抽样均一致",
    ))

    # 7 技能
    bsk = baseline.get("databases", {}).get("skills") or {}
    csk = cur.get("databases", {}).get("skills") or {}
    sk_diffs = []
    if bsk.get("skills") != csk.get("skills"):
        sk_diffs.append(f"数量 {bsk.get('skills')}->{csk.get('skills')}")
    sk_diff = _sample_diff(
        data / "skills.db", "skills", "name", "content",
        baseline.get("hashes", {}).get("skills") or [],
    )
    if sk_diff["mismatch"]:
        sk_diffs.append(f"抽样 {len(sk_diff['mismatch'])} 个正文变化")
    checks.append(_check(7, "技能（数量/抽样正文）", FAIL if sk_diffs else PASS,
                         "；".join(sk_diffs) or "数量与抽样均一致"))

    # 8 scene_key
    conn = _ro(data / "memory.db")
    unknown_list: list[str] = []
    ratio = 0.0
    total = c_total or 0
    if conn is not None:
        try:
            non_null = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE status='active' "
                "AND scene_key IS NOT NULL AND scene_key != ''"
            ).fetchone()[0])
            active = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE status='active'"
            ).fetchone()[0])
            ratio = (non_null / active) if active else 0.0
            if export_unknown:
                rows = conn.execute(
                    "SELECT memory_id FROM memories WHERE status='active' "
                    "AND (scene_key IS NULL OR scene_key = '')"
                ).fetchall()
                unknown_list = [str(r[0]) for r in rows]
        finally:
            conn.close()
    checks.append(_check(
        8, "scene_key 回填率（≥99%）", PASS if ratio >= 0.99 else FAIL,
        f"active={total} 非空率={ratio:.2%}（阶段 3 回填前应判 FAIL，属预期）",
    ))

    # 9 / 10 人工项
    checks.append(_check(
        9, "注入回归十条固定查询", MANUAL,
        "口径：两侧都关场景路（检索侧 scopes=['memory']），10 条固定查询"
        "（身份/家庭/健康/技术栈/状态/目标/角色/技能/知识/空分类）结果哈希差异率 ≤1%",
    ))
    checks.append(_check(
        10, "场景路移除影响清单", MANUAL,
        "口径：导出注入结果新增/消失 top 50 + 人工抽查 20 条，确认无关键信息丢失",
    ))

    summary = {
        "pass": sum(1 for c in checks if c["status"] == PASS),
        "fail": sum(1 for c in checks if c["status"] == FAIL),
        "warn": sum(1 for c in checks if c["status"] == WARN),
        "manual": sum(1 for c in checks if c["status"] == MANUAL),
    }
    report = {
        "kind": "sgme-2.0-migration-verification",
        "generated_at": _now_iso(),
        "baseline": baseline_path.name,
        "data_dir": data.name,
        "checks": checks,
        "summary": summary,
        "unknown_scene_key": unknown_list[:200],
        "strict": strict,
        "stage": int(stage),
    }
    if out_dir is None:
        out_dir = baseline_path.parent
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    label = datetime.now(UTC).strftime("%Y%m%d")
    json_path = out / f"2.0-迁移校验-{label}.json"
    md_path = out / f"2.0-迁移校验-{label}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(report), encoding="utf-8")
    report["_paths"] = {"json": str(json_path), "markdown": str(md_path)}
    return report


def _render_md(report: dict) -> str:
    lines = [f"# SGME 2.0 迁移校验（{report['baseline']}）", ""]
    s = report["summary"]
    lines.append(
        f"> 生成时间：{report['generated_at']} ｜ 迁移阶段 {report.get('stage', 0)} ｜ "
        f"PASS {s['pass']} / FAIL {s['fail']} / "
        f"WARN {s['warn']} / MANUAL {s['manual']} ｜ 口径：设计 §9.4 十项"
    )
    lines.append("")
    lines.append("| # | 项 | 判定 | 详情 |")
    lines.append("|---|---|---|---|")
    for c in report["checks"]:
        lines.append(f"| {c['no']} | {c['name']} | {c['status']} | {c['detail']} |")
    lines.append("")
    if report.get("unknown_scene_key"):
        lines.append("**scene_key 未回填清单（前 200 条）**：")
        lines.append("")
        lines.append("~~~")
        lines.extend(report["unknown_scene_key"])
        lines.append("~~~")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SGME 2.0 迁移校验十项（只读）")
    ap.add_argument("--baseline", default=None, help="基线 JSON（默认取 docs/migration 下最新）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    ap.add_argument("--export-unknown", action="store_true", help="导出 scene_key 未回填清单")
    ap.add_argument("--stage", type=int, default=0,
                    help="迁移阶段（0-4）；阶段 3 起修订史缺页由 WARN 升 FAIL（§9.4 #6）")
    args = ap.parse_args(argv)

    baseline = Path(args.baseline) if args.baseline else find_latest_baseline(
        ROOT / "docs" / "migration"
    )
    if baseline is None or not baseline.is_file():
        print("[FAIL] 找不到基线 JSON（先跑 scripts/migrate_2_0_baseline.py）", file=sys.stderr)
        return 1
    report = run(
        baseline, args.data_dir,
        out_dir=args.out_dir, strict=args.strict, export_unknown=args.export_unknown,
        stage=args.stage,
    )
    s = report["summary"]
    print(
        f"[{'FAIL' if s['fail'] or (args.strict and s['warn']) else 'OK'}] "
        f"PASS={s['pass']} FAIL={s['fail']} WARN={s['warn']} MANUAL={s['manual']} -> "
        f"{report['_paths']['markdown']}"
    )
    for c in report["checks"]:
        if c["status"] in (FAIL, WARN):
            print(f"  [{c['status']}] #{c['no']} {c['name']}: {c['detail']}")
    return 1 if (s["fail"] or (args.strict and s["warn"])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
