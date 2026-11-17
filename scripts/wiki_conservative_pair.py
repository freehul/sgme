# -*- coding: utf-8 -*-
"""scripts/wiki_conservative_pair.py：A-3 同题取代保守配对器（T-402 · 只读 dry-run）。

与 R1 审计（scripts/wiki_supersede_audit.py，T-388）的分工
----
- R1 = 指针链**完整性审计**（缺指针/断链/环/错链/链尾异常）；
- 本工具 = 同题取代**候选对发现**：产出 (旧页, 新页) 配对 + 置信度 + 三重判据逐条结果，
  供 T-404 wiki 整理清单使用。两者都只读，互不替代。

判据纪律（docs/plans/T-388-A-3-wiki取代范围定义-20261116.md，实现逐条钉死）
----
1. **同题精确相等 = 一级判据**（样本 4 对 B 全是同题双 hash）；同前缀只做**召回**
   （跨题候选一律 low 档交人工，宁漏不错）；
2. **标题内日期 token 不同 → 禁止配对**（跨日期台账是历史序列，真链在同题同日内）；
3. **方向校验一律用 ingested_at**（updated_at 会因 append/下线回写漂移）；
4. **指针核对**：显式指向候选新页 = explicit（strong）；缺失/自指 = 候选补链（medium）；
   指向第三页 = skip（错链归 R1 审计）；
5. **家族前缀防误配**：同前缀不同题须正文相似度过阈（≥PREFIX_SIM_MIN）才算 low 候选；
   公共前缀 <PREFIX_MIN_LEN（8 字符，cont* 纯伪影）不进召回桶；
6. draft 不是退休态（T-392E），不参与配对；C1 整组自指下线无后继 → 整组不配对。

机制钉子（范围定义 §三）
----
- ``supersedes`` 实测语义与字段名**相反**（旧页存的是后继页 id）——本工具按此语义核对；
- 自指指针 = 一等状态（无后继下线），不是脏数据；
- 同前缀分组自算（title[:PREFIX_MIN_LEN] 分桶，不复用库内分组字段）。

用法::

    python scripts/wiki_conservative_pair.py --db <wiki.db>                # 摘要
    python scripts/wiki_conservative_pair.py --db <wiki.db> --json out.json --csv out.csv
    python scripts/wiki_conservative_pair.py --db <wiki.db> --sample-prefix 发型库-无五

退出码：0 = 正常完成（有无候选均属正常结果）；2 = 用法/路径错误。永远 mode=ro，零写入。
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# 允许直接 `python scripts/xxx.py` 时从仓库根导入 sgme
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: 同前缀召回桶长度（公共前缀 <8 视为伪影，不进召回）
PREFIX_MIN_LEN = 8
#: 跨题（同前缀不同题）候选的正文相似度下限——SKILL 公共骨架相似度远低于此
PREFIX_SIM_MIN = 0.75
#: 相似度计算的正文截断（防超长页 O(n²)；原文比对口径与 T-388 采样一致取头部）
_SIM_SAMPLE_CHARS = 8000
#: 标题日期 token（YYYY-MM-DD）
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

PAIR_FIELDS = (
    "old_page_id", "old_title", "old_status", "old_ingested_at",
    "new_page_id", "new_title", "new_status", "new_ingested_at",
    "title_match", "shared_prefix_len", "content_similarity",
    "pointer_state", "confidence",
)

SKIP_REASONS = (
    "retired_group_no_successor",  # C1 整组自指下线（无后继即无对）
    "date_token_conflict",         # 跨日期台账（历史序列禁配）
    "pointer_points_elsewhere",    # 显式指针指向组内第三页（错链归 R1）
    "pointer_dangling",            # 显式指针指向库外页
    "direction_invalid",           # ingested_at 方向校验失败
    "similarity_below_threshold",  # 跨题候选相似度不过阈
)


def load_pages(conn: sqlite3.Connection) -> list[dict]:
    """读全量页面（含 content 供相似度；mode=ro 由调用方保证）。"""
    conn.row_factory = sqlite3.Row  # 调用方可能传裸连接（测试/复用），幂等自设
    rows = conn.execute(
        "SELECT page_id, title, status, supersedes, ingested_at, content FROM wiki_pages"
    ).fetchall()
    return [
        {
            "page_id": str(r["page_id"]),
            "title": str(r["title"] or ""),
            "status": str(r["status"] or ""),
            "supersedes": (str(r["supersedes"]).strip() if r["supersedes"] else ""),
            "ingested_at": str(r["ingested_at"] or ""),
            "content": str(r["content"] or ""),
        }
        for r in rows
    ]


def _date_tokens(title: str) -> set[str]:
    return set(_DATE_RE.findall(title))


def _shared_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _similarity(a: str, b: str) -> float:
    """正文相似度：快等短路 + 头部截断比对（零外部依赖）。"""
    if a == b:
        return 1.0
    sm = difflib.SequenceMatcher(None, a[:_SIM_SAMPLE_CHARS], b[:_SIM_SAMPLE_CHARS])
    if sm.real_quick_ratio() < PREFIX_SIM_MIN or sm.quick_ratio() < PREFIX_SIM_MIN:
        return sm.quick_ratio()  # 已注定不过阈，返回粗值即可
    return sm.ratio()


def _pair_record(old: dict, new: dict, title_match: str, sim: float,
                 pointer_state: str) -> dict:
    confidence = {
        ("exact", "explicit"): "high",
        ("exact", "missing"): "medium",
        ("exact", "self"): "medium",
        ("prefix", "explicit"): "low",
        ("prefix", "missing"): "low",
        ("prefix", "self"): "low",
    }[(title_match, pointer_state)]
    return {
        "old_page_id": old["page_id"], "old_title": old["title"],
        "old_status": old["status"], "old_ingested_at": old["ingested_at"],
        "new_page_id": new["page_id"], "new_title": new["title"],
        "new_status": new["status"], "new_ingested_at": new["ingested_at"],
        "title_match": title_match,
        "shared_prefix_len": _shared_prefix_len(old["title"], new["title"]),
        "content_similarity": round(sim, 4),
        "pointer_state": pointer_state,
        "confidence": confidence,
    }


def pair_pages(pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """配对主函数（纯函数，零 IO）：返回 (候选对, skip 明细)。

    两路召回（T-388 纪律）：
    - **一级 = 同题精确相等组**（不限标题长度，日期 token 冲突组内天然豁免——同题同日期）；
    - **二级 = 前缀桶跨题召回**（title[:8] 桶内不同题对，须相似度过阈，一律 low 档）。
    """
    pairs: list[dict] = []
    skipped: list[dict] = []
    by_id = {p["page_id"]: p for p in pages}
    published = [p for p in pages if p["status"] != "draft"]

    def _check_pair(old: dict, new: dict, title_match: str) -> None:
        """单对判据链（日期/方向/相似度），通过则 append 候选对。"""
        d_old, d_new = _date_tokens(old["title"]), _date_tokens(new["title"])
        if d_old and d_new and d_old != d_new:
            skipped.append({"old_page_id": old["page_id"],
                            "candidate_new_page_id": new["page_id"],
                            "reason": "date_token_conflict",
                            "detail": f"{sorted(d_old)} vs {sorted(d_new)}（跨日期=历史序列）"})
            return
        if old["ingested_at"] >= new["ingested_at"]:
            skipped.append({"old_page_id": old["page_id"],
                            "candidate_new_page_id": new["page_id"],
                            "reason": "direction_invalid",
                            "detail": "旧页 ingested_at 不早于新页"})
            return
        ptr = old["supersedes"]
        if ptr and ptr != old["page_id"]:
            ptr_state = "explicit" if ptr == new["page_id"] else "missing"
        else:
            ptr_state = "self" if ptr == old["page_id"] else "missing"
        sim = _similarity(old["content"], new["content"])
        if title_match == "prefix" and sim < PREFIX_SIM_MIN:
            skipped.append({"old_page_id": old["page_id"],
                            "candidate_new_page_id": new["page_id"],
                            "reason": "similarity_below_threshold",
                            "detail": f"同前缀不同题相似度 {sim:.2f} < {PREFIX_SIM_MIN}（家族兄弟）"})
            return
        pairs.append(_pair_record(old, new, title_match, sim, ptr_state))

    # ---------- 一级：同题精确相等组 ----------
    by_title: dict[str, list[dict]] = defaultdict(list)
    for p in published:
        if p["title"]:
            by_title[p["title"]].append(p)

    for title in sorted(by_title):
        grp = sorted(by_title[title], key=lambda p: p["ingested_at"])
        # C1 整组自指下线（无 active 且全部自指）→ 无后继即无对，整组跳过
        if all(p["supersedes"] == p["page_id"] for p in grp) and not any(
            p["status"] == "active" for p in grp
        ):
            for p in grp:
                skipped.append({"old_page_id": p["page_id"], "candidate_new_page_id": "",
                                "reason": "retired_group_no_successor", "detail": "C1 整组自指下线"})
            continue
        for old in grp:
            if old["status"] != "superseded":
                continue
            ptr = old["supersedes"]
            later = [p for p in grp if p["page_id"] != old["page_id"]
                     and p["ingested_at"] > old["ingested_at"]]
            if ptr and ptr != old["page_id"]:
                target = by_id.get(ptr)
                if target is None:
                    skipped.append({"old_page_id": old["page_id"], "candidate_new_page_id": "",
                                    "reason": "pointer_dangling", "detail": f"指针指向库外页 {ptr}"})
                    continue
                if target not in grp:
                    skipped.append({"old_page_id": old["page_id"], "candidate_new_page_id": "",
                                    "reason": "pointer_points_elsewhere",
                                    "detail": "指针指向组外页（错链归 R1 审计）"})
                    continue
                _check_pair(old, target, "exact")
                # 组内其余更晚页 = 未被指针选中的候选，记录跳过原因（审计可见）
                for p in later:
                    if p["page_id"] != target["page_id"]:
                        skipped.append({"old_page_id": old["page_id"],
                                        "candidate_new_page_id": p["page_id"],
                                        "reason": "pointer_points_elsewhere",
                                        "detail": "旧页显式指针指向组内另一页"})
            else:
                # 缺指针/自指：与紧邻后继（ingested_at 最早的更晚页）配对（链式）
                if later:
                    _check_pair(old, later[0], "exact")

    # ---------- 二级：前缀桶跨题召回（不同题才进这路，同题已在一级处理） ----------
    buckets: dict[str, list[dict]] = defaultdict(list)
    for p in published:
        if len(p["title"]) >= PREFIX_MIN_LEN:
            buckets[p["title"][:PREFIX_MIN_LEN]].append(p)

    for key in sorted(buckets):
        bucket = buckets[key]
        # 桶内同题组已在一级处理，这里只取跨题对
        for i, old in enumerate(bucket):
            if old["status"] != "superseded":
                continue
            for new in bucket[i + 1:]:
                if new["title"] == old["title"] or new["ingested_at"] <= old["ingested_at"]:
                    continue
                _check_pair(old, new, "prefix")

    return pairs, skipped


def run(db_path: Path) -> dict:
    """入口：返回回执 dict（零写入）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        pages = load_pages(conn)
    finally:
        conn.close()
    pairs, skipped = pair_pages(pages)
    by_conf: dict[str, int] = defaultdict(int)
    by_match: dict[str, int] = defaultdict(int)
    by_ptr: dict[str, int] = defaultdict(int)
    for p in pairs:
        by_conf[p["confidence"]] += 1
        by_match[p["title_match"]] += 1
        by_ptr[p["pointer_state"]] += 1
    by_skip: dict[str, int] = defaultdict(int)
    for s in skipped:
        by_skip[s["reason"]] += 1
    return {
        "total_pages": len(pages),
        "pairs": len(pairs),
        "by_confidence": dict(by_conf),
        "by_title_match": dict(by_match),
        "by_pointer_state": dict(by_ptr),
        "skipped": len(skipped),
        "by_skip_reason": dict(by_skip),
        "items": pairs,
        "skip_items": skipped,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="A-3 同题取代保守配对器（只读 dry-run，T-402）")
    ap.add_argument("--db", default=None, help="wiki.db 路径（缺省取 sgme 配置 DATA_DIR/wiki.db）")
    ap.add_argument("--json", default=None, help="候选对输出 JSON 路径（含 skip 明细）")
    ap.add_argument("--csv", default=None, help="候选对输出 CSV 路径（喂 T-404）")
    ap.add_argument("--sample-prefix", default=None,
                    help="只统计该前缀桶（30 条抽样验收用，缺省全库）")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.db:
        db_path = Path(args.db)
    else:
        from sgme import config as sgme_config

        db_path = Path(sgme_config.DATA_DIR) / "wiki.db"
    if not db_path.exists():
        print(f"wiki.db 不存在: {db_path}（用 --db 显式指定）", file=sys.stderr)
        return 2

    report = run(db_path)
    items = report["items"]
    if args.sample_prefix:
        items = [p for p in items if p["old_title"].startswith(args.sample_prefix)
                 or p["new_title"].startswith(args.sample_prefix)]
        report = {**report, "items": items,
                  "pairs": len(items),
                  "by_confidence": {}, "by_title_match": {}, "by_pointer_state": {}}
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(PAIR_FIELDS))
            w.writeheader()
            w.writerows(items)
    print(
        f"pages={report['total_pages']} 候选对={report['pairs']} "
        f"skip={report['skipped']}"
    )
    print(f"by_confidence: {report.get('by_confidence', {})}")
    print(f"by_title_match: {report.get('by_title_match', {})}")
    print(f"by_pointer_state: {report.get('by_pointer_state', {})}")
    print(f"by_skip_reason: {report.get('by_skip_reason', {})}")
    for p in items[:10]:
        print(f"  [{p['confidence']}/{p['title_match']}/{p['pointer_state']}] "
              f"{p['old_page_id'][:8]}({p['old_status']}) → {p['new_page_id'][:8]}({p['new_status']}) "
              f"{p['old_title'][:24]} sim={p['content_similarity']}")
    if len(items) > 10:
        print(f"  ...（其余 {len(items) - 10} 条见 --json/--csv）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
