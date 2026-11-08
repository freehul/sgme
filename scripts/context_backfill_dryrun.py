#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""W11-W12 存量上下文回填 dry-run（**只读分析，绝不写库**）。

背景（docs/design/SGME2.0-上下文归属与因果边-v0.1.md W11/W12）：
- 生产基线（2026-11-08 文档）：active 40,718；l2_facts_json 非空 17,289；其中有 causes 11,325；
  memory_edges 735（similar 697 / contradicts 27 / causes 11）；按钮/黑白相关 active 93。
- 2026-11-08 快照实测（memory.db.bak-20261108-context）：
  active 40,718 ✅ / l2 非空 18,158 / 有 causes 12,004 行 / causes 13,225 对 / edges 735 ✅
  （l2 与 causes 比文档多几百，属文档记录时点之后的新写入）。
- A2（causes 落 memory_edges）历史上未落地。本脚本评估两件事：
  1) 存量 causes 用 W04 映射器（单元内归一文本 → memory_id）能映射多少——**结论：精确口径 0%**
     （causes 是因果摘要句，与任何记忆 content 都不逐字相等；详见报告中 paraphrase 证据）；
  2) 「按钮/黑白」93 条 + 全库近重复聚类，给出「保留 / 归档」**建议**（不执行）。
     注意 W02 的 93 口径会把「白屏」类 UI bug 复盘混进来（实测 40/93），故分析集另用配色词口径（46 条）。

纪律：
- 默认且只支持 dry-run：连接一律 ``mode=ro`` + ``PRAGMA query_only=1``，无 --apply；
- 读的是**快照**（推荐 `memory.db.bak-*`，无 WAL，可 ``immutable=1``）；
- 零 LLM、零网络、纯词法（bigram jaccard）；近重复建议是**词法近似**，非向量口径审批。

用法：
    # 本地对快照跑（只读）
    python scripts/context_backfill_dryrun.py --db <path>/memory.db.bak-20261108-context \
        --mode all --json-out tmp/dryrun.json --report-out tmp/dryrun.md

    # NAS 容器内跑（脚本从 stdin 传入，只输出 ASCII JSON）
    Get-Content scripts/context_backfill_dryrun.py -Raw | ssh nas \
        "docker exec -i sgme python - --db /data/data/memory.db.bak-20261108-context --stdout-json"

    # 本地把容器产出的 JSON 渲染成 Markdown
    python scripts/context_backfill_dryrun.py --render-json tmp/dryrun.json --report-out tmp/dryrun.md

模块结构（纯函数，可直接单测）：
- 文本归一 / bigram / jaccard（对齐 engine.l2_edges.normalize_unit_text 与词法近重复）
- analyze_baseline / analyze_causes / analyze_near_dup / render_markdown
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

# ---------- 常量（与生产口径对齐） ----------

#: 关系归一文本的空白折叠（同 engine/l2_edges.normalize_unit_text）
_WS_RE = re.compile(r"\s+")
#: 去首尾标点字符集（与 engine/l2_edges._TRIM_PUNCT 同源，只取常用集）
_TRIM_PUNCT = (
    " \t\r\n\u3000.,;:!?。，；：！？、…·"
    "\"'“”‘’「」『』（）()[]【】《》〈〉"
)
_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)

#: 文档基线（2026-11-08，仅用于报告对账，不参与判定）
BASELINE = {
    "active": 40_718,
    "l2_facts_json_non_empty": 17_289,
    "with_causes": 11_325,
    "edges_total": 735,
}

#: 按钮类评估集 SQL —— **W02 基线口径**（`按钮 AND 白`，2026-11-08 快照复现 93 条）
#: ⚠️ 该口径会把「白屏」类 UI bug 复盘一起捞进来（实测 93 条里 40 条是白屏，见 baseline_whitescreen），
#: 故真正做保留/归档分析用下面的 DEFAULT_BUTTON_ANALYSIS_SQL（配色词口径，46 条）。
DEFAULT_BUTTON_SQL = "content LIKE '%按钮%' AND content LIKE '%白%'"
#: 按钮**配色规则**口径（按钮 + 白字/黑字/对比度/实色底/白色字）——分析集
DEFAULT_BUTTON_ANALYSIS_SQL = (
    "content LIKE '%按钮%' AND (content LIKE '%白字%' OR content LIKE '%黑字%'"
    " OR content LIKE '%对比度%' OR content LIKE '%实色底%' OR content LIKE '%白色字%')"
)
#: 按钮类关键词分项计数（仅报告用，不参与筛选）
DEFAULT_BUTTON_KEYWORDS = ("按钮", "白字", "黑字", "对比度", "实色底", "白色字")
#: 相似度分层（报告里逐级给出「到哪一步能归并」）
DEFAULT_SIM_LEVELS = (0.92, 0.85, 0.6, 0.4)

#: 近重复词法阈值（生产 near_dup: threshold=0.92 / gray_zone=0.85）
DEFAULT_THRESHOLD = 0.92
DEFAULT_GRAY_ZONE = 0.85

#: 全库 blocking：只把 doc frequency ≤ 该值的 bigram 当桶（防高频桶爆炸）
_BLOCK_DF_MAX = 200
#: 全库 blocking：每条记忆取最稀有的 K 个 bigram 做候选来源
_BLOCK_KEYS_PER_DOC = 4
#: 全库候选对上限（防御性；超限截断并在报告里标注）
_MAX_CANDIDATE_PAIRS = 3_000_000


# ---------- 文本工具（纯函数） ----------

def normalize_text(text: Any) -> str:
    """单元内文本归一：strip → 折叠空白 → 去首尾标点 → ASCII 小写。

    与 engine/l2_edges.normalize_unit_text 同口径（W01 §3.1）——映射率统计必须
    用**写入侧同一归一**，否则统计出来的覆盖率不可信。
    """
    s = str(text) if text is not None else ""
    if not s:
        return ""
    s = _WS_RE.sub(" ", s).strip()
    s = s.strip(_TRIM_PUNCT)
    return s.translate(_ASCII_LOWER)


def bigrams(text: str) -> set[str]:
    """字符级 bigram 集合（归一后去空白；单字文本退化为单字集合）。"""
    s = re.sub(r"\s+", "", normalize_text(text))
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度；任一为空 → 0.0（空内容不视作相同）。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def parse_l2_facts(raw: Any) -> dict[str, list]:
    """l2_facts_json → {"facts": [...], "causes": [{"from","to"}]}（容错，坏 JSON → 空）。"""
    empty: dict[str, list] = {"facts": [], "causes": []}
    if not raw:
        return empty
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return empty
    if isinstance(data, list):
        data = {"facts": data}
    if not isinstance(data, dict):
        return empty
    facts = [f for f in (data.get("facts") or []) if isinstance(f, dict)]
    causes = []
    for c in data.get("causes") or []:
        if not isinstance(c, dict):
            continue
        frm, to = c.get("from"), c.get("to")
        if normalize_text(frm) and normalize_text(to):
            causes.append({"from": frm, "to": to})
    return {"facts": facts, "causes": causes}


# ---------- 只读连接 ----------

def open_db_readonly(path: str | Path) -> sqlite3.Connection:
    """只读打开 SQLite（mode=ro + immutable=1 + query_only）。

    - ``immutable=1``：不建 WAL/SHM、不加锁——对 `.bak` 快照最安全；
    - ``PRAGMA query_only=1``：双保险，任何写尝试都会被 SQLite 拒绝；
    - WAL 活动库请先 `.backup` 再分析（本函数对旧库 WAL 不可见）。
    """
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"数据库不存在: {p}")
    conn = sqlite3.connect(f"file:{quote(str(p))}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


# ---------- 1. 基线对账 ----------

def analyze_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    """读基线数字（与 W02 文档对账；不修改任何东西）。"""
    active = conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE status='active'"
    ).fetchone()["c"]
    l2_non_empty = conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE l2_facts_json IS NOT NULL "
        "AND l2_facts_json != ''"
    ).fetchone()["c"]
    with_causes = 0
    causes_total = 0
    bad_json = 0
    rows = conn.execute(
        "SELECT l2_facts_json FROM memories WHERE l2_facts_json IS NOT NULL "
        "AND l2_facts_json != ''"
    ).fetchall()
    for row in rows:
        raw = row["l2_facts_json"]
        try:
            json.loads(raw) if isinstance(raw, str) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            bad_json += 1
            continue
        causes = parse_l2_facts(raw)["causes"]
        if causes:
            with_causes += 1
            causes_total += len(causes)
    edges: dict[str, int] = {}
    edges_total = 0
    if _table_exists(conn, "memory_edges"):
        for r in conn.execute(
            "SELECT relation, COUNT(*) AS c FROM memory_edges GROUP BY relation"
        ):
            edges[r["relation"]] = r["c"]
            edges_total += r["c"]
    return {
        "active": active,
        "l2_facts_json_non_empty": l2_non_empty,
        "with_causes": with_causes,
        "causes_total": causes_total,
        "l2_bad_json": bad_json,
        "edges_total": edges_total,
        "edges_by_relation": edges,
        "baseline_doc": BASELINE,
    }


# ---------- 2. 存量 causes 可映射率（W04 映射器 dry-run） ----------

def _norm_index(rows: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """[(memory_id, content)] → {归一文本: [memory_id...]}（多 id = 歧义）。"""
    index: dict[str, list[str]] = {}
    for mid, content in rows:
        key = normalize_text(content)
        if not key:
            continue
        bucket = index.setdefault(key, [])
        if mid not in bucket:
            bucket.append(mid)
    return index


def _classify_endpoint(
    key: str, index: dict[str, list[str]], *, self_id: str | None = None
) -> tuple[str, str | None]:
    """归一文本 → ("ok"|"missing"|"ambiguous", memory_id)。"""
    if not key:
        return "missing", None
    bucket = index.get(key) or []
    if not bucket:
        return "missing", None
    if len(bucket) > 1:
        # 同文本多记忆：映射器优先 main，但存量 dry-run 无 main 信息 → 记歧义（不猜）
        return "ambiguous", None
    return "ok", bucket[0]


def analyze_causes(conn: sqlite3.Connection, *, max_samples: int = 8) -> dict[str, Any]:
    """存量 causes 可映射率（三级口径，从严格到宽松，全部确定性精确文本匹配）。

    存量（2.0 A2 落地前）没有 ``_l2_unit`` 元数据、也没有父边，单元边界不可精确还原，
    因此给三级阶梯，让 lead 按风险选口径：
    1. ``unit_strict``：只把**本条主干记忆自己的 content** 当单元（最保守，下界）；
    2. ``same_source``：同 source_ref（同一 L0 文件/批次）内 active 记忆精确同文本；
    3. ``global_exact``：全库 active 记忆精确同文本（上界；精确匹配、非语义猜测）。

    每级统计：mappable（两端都命中且不是同一条）/ 失败原因 / 覆盖率；
    另给「至少一条 causes 可映射的 l2 记录数」与不可映射样例（供人工判断值不值得回填）。
    """
    l2_rows = conn.execute(
        "SELECT memory_id, content, status, l2_facts_json FROM memories "
        "WHERE l2_facts_json IS NOT NULL AND l2_facts_json != ''"
    ).fetchall()

    active_rows = conn.execute(
        "SELECT memory_id, content FROM memories WHERE status='active'"
    ).fetchall()
    global_index = _norm_index((r["memory_id"], r["content"]) for r in active_rows)

    # source_ref → {归一文本: [memory_id]}（同文件/批次作用域）
    source_index: dict[str, dict[str, list[str]]] = {}
    source_refs_by_mem: dict[str, set[str]] = {}
    if _table_exists(conn, "memory_sources"):
        rows = conn.execute(
            "SELECT ms.memory_id AS memory_id, ms.source_ref AS source_ref, "
            "m.content AS content, m.status AS status "
            "FROM memory_sources ms JOIN memories m ON m.memory_id = ms.memory_id "
            "WHERE m.status='active'"
        ).fetchall()
        for r in rows:
            src = r["source_ref"] or ""
            if not src:
                continue
            source_refs_by_mem.setdefault(r["memory_id"], set()).add(src)
            src_idx = source_index.setdefault(src, {})
            key = normalize_text(r["content"])
            if key:
                bucket = src_idx.setdefault(key, [])
                if r["memory_id"] not in bucket:
                    bucket.append(r["memory_id"])

    levels = ("unit_strict", "same_source", "global_exact")
    stats: dict[str, dict[str, Any]] = {
        lv: {
            "mappable": 0, "self": 0, "invalid": 0, "same_id": 0,
            "from_missing": 0, "to_missing": 0, "both_missing": 0,
            "from_ambiguous": 0, "to_ambiguous": 0,
            "rows_with_any_mappable": 0,
            "samples_unmapped": [],
        }
        for lv in levels
    }

    total_causes = 0
    total_rows = 0
    rows_with_causes = 0
    for row in l2_rows:
        total_rows += 1
        mid = row["memory_id"]
        own_key = normalize_text(row["content"])
        strict_index = {own_key: [mid]} if own_key else {}
        # 同 source_ref 作用域索引（一个记录可能挂多个来源；合并去重一次，避免逐 causes 重建）
        merged_source: dict[str, list[str]] = {}
        for src in source_refs_by_mem.get(mid, ()):
            for key, ids in source_index.get(src, {}).items():
                bucket = merged_source.setdefault(key, [])
                for i in ids:
                    if i not in bucket:
                        bucket.append(i)
        parsed = parse_l2_facts(row["l2_facts_json"])
        if not parsed["causes"]:
            continue
        rows_with_causes += 1
        per_row_mappable: dict[str, int] = {lv: 0 for lv in levels}
        for cause in parsed["causes"]:
            total_causes += 1
            nf, nt = normalize_text(cause["from"]), normalize_text(cause["to"])
            if nf == nt:
                for lv in levels:
                    stats[lv]["self"] += 1
                continue
            for lv in levels:
                if lv == "unit_strict":
                    st = stats[lv]
                    sf, sid = _classify_endpoint(nf, strict_index)
                    st_t, tid = _classify_endpoint(nt, strict_index)
                elif lv == "same_source":
                    st = stats[lv]
                    sf, sid = _classify_endpoint(nf, merged_source)
                    st_t, tid = _classify_endpoint(nt, merged_source)
                else:
                    st = stats[lv]
                    sf, sid = _classify_endpoint(nf, global_index)
                    st_t, tid = _classify_endpoint(nt, global_index)

                if sf == "ok" and st_t == "ok":
                    if sid == tid:
                        st["same_id"] += 1
                        continue
                    st["mappable"] += 1
                    per_row_mappable[lv] += 1
                    continue
                # 失败原因归类（from 优先，其次 to）
                for status_val, side in ((sf, "from"), (st_t, "to")):
                    if status_val == "ok":
                        continue
                    if status_val == "ambiguous":
                        st[f"{side}_ambiguous"] += 1
                        break
                    # missing
                    if side == "from" and st_t == "missing":
                        st["both_missing"] += 1
                    else:
                        st[f"{side}_missing"] += 1
                    _append_sample(st, mid, cause, level=lv, max_samples=max_samples)
                    break
        for lv in levels:
            if per_row_mappable[lv]:
                stats[lv]["rows_with_any_mappable"] += 1

    out: dict[str, Any] = {
        "l2_rows": total_rows,
        "rows_with_causes": rows_with_causes,
        "causes_total": total_causes,
        "levels": {},
    }
    for lv in levels:
        st = stats[lv]
        st["rate"] = (st["mappable"] / total_causes) if total_causes else 0.0
        st["rows_rate"] = (
            st["rows_with_any_mappable"] / rows_with_causes if rows_with_causes else 0.0
        )
        out["levels"][lv] = st
    # 为什么映射不上：causes 文本与记忆原文的距离（决定回填可行性的关键证据）
    out["paraphrase"] = analyze_paraphrase(conn)
    return out


def analyze_paraphrase(conn: sqlite3.Connection, *, scope_cap: int = 200) -> dict[str, Any]:
    """量化「causes 文本 vs 记忆原文」的距离（判断回填可行性用）。

    存量 causes 的 from/to 是 L2 模型写的**因果摘要句**，而记忆 content 是被压缩/截断
    的正文。两者是否可映射直接决定 W11 回填可行性，这里给三类证据：
    1. 单元内精确命中：端点 == 本单元 main 内容 / 任一 facts 内容 的占比；
    2. 单元内包含关系：端点与本单元文本互为子串的占比（说明是缩写而非原文）；
    3. 同 source_ref 内最佳词法相似度（bigram jaccard）分布 —— 若普遍很低，说明
       连词法近似匹配都不可行（语义向量口径本 dry-run 不评估：零 embed 成本约束）。

    scope_cap：单 source_ref scope 过大（如 7,943 条/文件）时跳过相似度计算并计数
    （防 O(n×scope) 爆炸）；精确/包含两项不受该限制。
    """
    rows = conn.execute(
        "SELECT m.memory_id AS memory_id, m.content AS content, "
        "m.l2_facts_json AS l2, ms.source_ref AS source_ref "
        "FROM memories m LEFT JOIN memory_sources ms ON ms.memory_id = m.memory_id "
        "WHERE m.l2_facts_json IS NOT NULL AND m.l2_facts_json != '' "
        "ORDER BY COALESCE(ms.source_ref, '')"
    ).fetchall()
    # 同一记忆多 source 行 → 保留第一条（按 source_ref 排序后的首个）
    by_mem: dict[str, sqlite3.Row] = {}
    for r in rows:
        by_mem.setdefault(r["memory_id"], r)

    endpoints = 0
    unit_exact = 0
    unit_contained = 0
    rows_with_causes = 0
    sim_rows = 0
    sim_skipped = 0
    sim_endpoints = 0
    rows_scope_sampled = 0
    dist = {"<0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, ">=0.6": 0}
    # 模糊可映射上界（词法口径）：端点取同 scope 最佳相似且非自身
    fuzzy_thresholds = (0.4, 0.6, 0.8)
    fuzzy_mappable = {f"ge_{str(t).replace('.', '_')}": 0 for t in fuzzy_thresholds}

    scope_key: str | None = None
    scope_sig: list[tuple[str, set[str]]] = []
    scope_ok = False
    scope_sampled = False

    for mid, r in by_mem.items():
        src = r["source_ref"] or ""
        if src != scope_key:
            # 行已按 source_ref 排序 → 顺序扫描时同一 scope 连续出现，只需持有当前 scope；
            # 不建全局缓存（141 个千条级 scope × bigram 集合会吃掉大量内存）。
            scope_key = src
            scope_sig = []
            scope_ok = False
            scope_sampled = False
            if src:
                items = conn.execute(
                    "SELECT m.memory_id AS memory_id, m.content AS content "
                    "FROM memory_sources ms JOIN memories m "
                    "ON m.memory_id = ms.memory_id "
                    "WHERE ms.source_ref=? AND m.status='active'",
                    (src,),
                ).fetchall()
                ordered = sorted(items, key=lambda it: it["memory_id"])
                if len(ordered) > scope_cap:
                    # 超大 scope（单文件 7,943 条等）：确定性等距抽样到 scope_cap；
                    # 偏差方向：最佳匹配可能被抽样漏掉 → 相似度偏低估（保守）
                    stride = (len(ordered) + scope_cap - 1) // scope_cap
                    ordered = ordered[::max(stride, 1)][:scope_cap]
                    scope_sampled = True
                scope_ok = True
                scope_sig = [(it["memory_id"], bigrams(it["content"])) for it in ordered]

        parsed = parse_l2_facts(r["l2"])
        causes = parsed["causes"]
        if not causes:
            continue
        rows_with_causes += 1
        if scope_ok and scope_sampled:
            rows_scope_sampled += 1
        unit_texts = [normalize_text(r["content"])]
        for f in parsed["facts"]:
            t = normalize_text(f.get("content"))
            if t:
                unit_texts.append(t)
        # 本行端点的最佳相似（供 fuzzy 判定）
        best: dict[str, list[tuple[float, str]]] = {"from": [], "to": []}
        for cause in causes:
            for side in ("from", "to"):
                key = normalize_text(cause[side])
                if not key:
                    continue
                endpoints += 1
                if any(key == t for t in unit_texts if t):
                    unit_exact += 1
                elif any(t and (key in t or t in key) for t in unit_texts):
                    unit_contained += 1
                if not scope_ok:
                    sim_skipped += 1
                    continue
                sig = bigrams(key)
                top: list[tuple[float, str]] = []
                for oid, osig in scope_sig:
                    if oid == mid:
                        continue
                    top.append((jaccard(sig, osig), oid))
                top.sort(key=lambda x: -x[0])
                sim_endpoints += 1
                if top:
                    s = top[0][0]
                    if s < 0.2:
                        dist["<0.2"] += 1
                    elif s < 0.4:
                        dist["0.2-0.4"] += 1
                    elif s < 0.6:
                        dist["0.4-0.6"] += 1
                    else:
                        dist[">=0.6"] += 1
                best[side].append((top[0][0] if top else 0.0,
                                   top[0][1] if top else ""))
            if len(best["from"]) and len(best["to"]):
                bf, idf = best["from"][-1]
                bt, idt = best["to"][-1]
                if idf and idt and idf != idt:
                    for t in fuzzy_thresholds:
                        if bf >= t and bt >= t:
                            fuzzy_mappable[f"ge_{str(t).replace('.', '_')}"] += 1
        if scope_ok:
            sim_rows += 1

    return {
        "scope_cap": scope_cap,
        "rows_with_causes": rows_with_causes,
        "endpoints": endpoints,
        "unit_exact_hits": unit_exact,
        "unit_exact_rate": (unit_exact / endpoints) if endpoints else 0.0,
        "unit_contained_hits": unit_contained,
        "unit_contained_rate": (unit_contained / endpoints) if endpoints else 0.0,
        "same_source_best_sim": {
            "rows_evaluated": sim_rows,
            "rows_scope_sampled": rows_scope_sampled,
            "rows_not_evaluated": rows_with_causes - sim_rows,
            "endpoints_evaluated": sim_endpoints,
            "endpoints_skipped": sim_skipped,
            "distribution": dist,
            "fuzzy_mappable_pairs": fuzzy_mappable,
        },
        "note": (
            "endpoints 是 L2 因果摘要句、记忆 content 是压缩正文——精确命中率即 W04 "
            "映射器的上限；同 source 词法相似度普遍偏低时，词法模糊回填同样不可行。"
            "语义向量口径未评估（dry-run 零 embed 成本约束）。"
        ),
    }


def _append_sample(st: dict[str, Any], memory_id: str, cause: dict, *,
                   level: str, max_samples: int) -> None:
    if len(st["samples_unmapped"]) >= max_samples:
        return
    st["samples_unmapped"].append({
        "level": level,
        "memory_id": memory_id,
        "from": str(cause.get("from") or "")[:80],
        "to": str(cause.get("to") or "")[:80],
    })


# ---------- 3. near-dup 聚类 dry-run ----------

def _load_active_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """active 记忆 → 聚类条目（id/内容/长度/时间/是否带 causes）。"""
    rows = conn.execute(
        "SELECT memory_id, content, created_at, updated_at, l2_facts_json "
        "FROM memories WHERE status='active'"
    ).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        causes = parse_l2_facts(r["l2_facts_json"])["causes"]
        items.append({
            "memory_id": r["memory_id"],
            "content": content,
            "norm": normalize_text(content),
            "length": len(content),
            "created_at": r["created_at"] or "",
            "updated_at": r["updated_at"] or "",
            "has_l2": bool(r["l2_facts_json"]),
            "has_causes": bool(causes),
            "causes_count": len(causes),
        })
    return items


def recommend_keep(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """簇内保留建议：**带 causes 优先**，其次最长，再其次最新。

    返回 {"keep": item, "archive": [...], "reason": str}（只给建议，不执行）。
    """
    ranked = sorted(
        cluster,
        key=lambda it: (it["has_causes"], it["length"], it["updated_at"]),
        reverse=True,
    )
    keep = ranked[0]
    if keep["has_causes"]:
        reason = "带 causes（因果上下文最完整）"
    elif keep["length"] > 0:
        reason = "无 causes，取最长内容（信息量最大）"
    else:
        reason = "无可用内容差异，取最新"
    return {
        "keep_memory_id": keep["memory_id"],
        "keep_reason": reason,
        "archive_memory_ids": [it["memory_id"] for it in ranked[1:]],
    }


def _cluster_all_pairs(items: list[dict[str, Any]], threshold: float) -> list[list[dict]]:
    """贪心聚类（O(n·簇)）：与簇代表 sim ≥ threshold 即入簇；代表取簇内首个。"""
    sig = {it["memory_id"]: bigrams(it["content"]) for it in items}
    clusters: list[dict[str, Any]] = []
    for it in items:
        joined = False
        for cl in clusters:
            rep = cl["items"][0]
            if jaccard(sig[it["memory_id"]], sig[rep["memory_id"]]) >= threshold:
                cl["items"].append(it)
                joined = True
                break
        if not joined:
            clusters.append({"items": [it]})
    return [cl["items"] for cl in clusters if len(cl["items"]) > 1]


def _cluster_blocked(items: list[dict[str, Any]], threshold: float,
                     *, df_max: int = _BLOCK_DF_MAX,
                     keys_per_doc: int = _BLOCK_KEYS_PER_DOC,
                     max_pairs: int = _MAX_CANDIDATE_PAIRS) -> tuple[list[list[dict]], dict[str, Any]]:
    """全库近重复聚类（blocking + union-find，O(候选对)）。

    blocking：字符 bigram 倒排，只用 doc frequency ≤ df_max 的稀有 bigram 组桶，
    每条记忆取最稀有的 keys_per_doc 个桶与他人比较 → 候选对规模可控；
    候选对再算 jaccard ≥ threshold 连边，union-find 得簇。
    """
    sig = {it["memory_id"]: bigrams(it["content"]) for it in items}
    df: dict[str, int] = {}
    for b in sig.values():
        for g in b:
            df[g] = df.get(g, 0) + 1
    buckets: dict[str, list[int]] = {}
    for idx, it in enumerate(items):
        keys = [g for g in sig[it["memory_id"]] if df.get(g, 0) <= df_max]
        keys.sort(key=lambda g: (df.get(g, 0), g))
        for g in keys[:keys_per_doc]:
            buckets.setdefault(g, []).append(idx)

    pairs: set[tuple[int, int]] = set()
    truncated = False
    for idxs in buckets.values():
        n = len(idxs)
        if n < 2:
            continue
        for i in range(n):
            for j in range(i + 1, n):
                pairs.add((idxs[i], idxs[j]) if idxs[i] < idxs[j] else (idxs[j], idxs[i]))
                if len(pairs) > max_pairs:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    edges = 0
    for i, j in pairs:
        a, b = items[i], items[j]
        if jaccard(sig[a["memory_id"]], sig[b["memory_id"]]) >= threshold:
            union(i, j)
            edges += 1
    groups: dict[int, list[dict]] = {}
    for idx, it in enumerate(items):
        groups.setdefault(find(idx), []).append(it)
    clusters = [g for g in groups.values() if len(g) > 1]
    meta = {
        "items": len(items),
        "df_buckets": len(buckets),
        "candidate_pairs": len(pairs),
        "sim_edges": edges,
        "pairs_truncated": truncated,
    }
    return clusters, meta


def _pair_sim_stats(items: list[dict[str, Any]],
                     thresholds: Iterable[float] = DEFAULT_SIM_LEVELS) -> dict[str, Any]:
    """小集合全对相似度统计（按钮类 93 条级，O(n²)）。"""
    levels = list(thresholds)
    sig = [(it["memory_id"], bigrams(it["content"])) for it in items]
    pairs_total = 0
    max_sim = 0.0
    counts = {_level_key(t): 0 for t in levels}
    for i in range(len(sig)):
        for j in range(i + 1, len(sig)):
            s = jaccard(sig[i][1], sig[j][1])
            pairs_total += 1
            max_sim = max(max_sim, s)
            for t in levels:
                if s >= t:
                    counts[_level_key(t)] += 1
    return {
        "pairs_total": pairs_total,
        "max_sim": round(max_sim, 4),
        "pair_counts": counts,
    }


def _cluster_counts_by_threshold(items: list[dict[str, Any]],
                                thresholds: Iterable[float]) -> dict[str, Any]:
    """各阈值下的贪心簇数/成员数（说明「词法口径能归到哪一步」）。"""
    out: dict[str, Any] = {}
    for t in thresholds:
        clusters = _cluster_all_pairs(items, t)
        out[_level_key(t)] = {
            "clusters": len(clusters),
            "members": sum(len(cl) for cl in clusters),
        }
    return out


def _level_key(t: float) -> str:
    return f"ge_{str(t).replace('.', '_')}"


def analyze_near_dup(
    conn: sqlite3.Connection,
    *,
    button_sql: str = DEFAULT_BUTTON_SQL,
    button_analysis_sql: str = DEFAULT_BUTTON_ANALYSIS_SQL,
    threshold: float = DEFAULT_THRESHOLD,
    gray_zone: float = DEFAULT_GRAY_ZONE,
    sample_groups: int = 100,
    seed: int = 20_261_108,
) -> dict[str, Any]:
    """按钮类（W02 口径核对 + 配色规则口径分析）+ 全库近重复聚类（dry-run，仅建议）。"""
    items = _load_active_items(conn)
    by_id = {it["memory_id"]: it for it in items}

    # ---- 按钮类：先复现 W02「按钮/黑白相关 active 93」做对账，再用配色口径做分析 ----
    def _ids(sql: str) -> set[str]:
        return {
            r["memory_id"]
            for r in conn.execute(
                f"SELECT memory_id FROM memories WHERE status='active' AND ({sql})"
            )
        }

    baseline_ids = _ids(button_sql)
    baseline_items = [by_id[mid] for mid in baseline_ids if mid in by_id]
    button_ids = _ids(button_analysis_sql)
    button_items = [by_id[mid] for mid in button_ids if mid in by_id]
    baseline_whitescreen = conn.execute(
        f"SELECT COUNT(*) AS c FROM memories WHERE status='active' AND ({button_sql}) "
        "AND content LIKE '%白屏%'"
    ).fetchone()["c"]
    kw_counts: dict[str, int] = {}
    for kw in DEFAULT_BUTTON_KEYWORDS:
        kw_counts[kw] = conn.execute(
            "SELECT COUNT(*) AS c FROM memories WHERE status='active' AND content LIKE ?",
            (f"%{kw}%",),
        ).fetchone()["c"]

    levels = (threshold, gray_zone, 0.6, 0.4)
    pair_stats = _pair_sim_stats(button_items, levels)
    cluster_stats = _cluster_counts_by_threshold(button_items, levels)
    strict_clusters = _cluster_all_pairs(button_items, gray_zone)
    review_clusters = _cluster_all_pairs(button_items, 0.4)  # 弱分组：仅人工复核参考
    review_payload = _clusters_payload(
        _sort_clusters(review_clusters), threshold=threshold, gray_zone=0.4)
    keep_candidates = [it for it in button_items if it["has_causes"]]
    bare_candidates = [it for it in button_items if not it["has_causes"]]
    baseline_pair_stats = _pair_sim_stats(
        baseline_items, (threshold, gray_zone, 0.6, 0.4))
    button_out: dict[str, Any] = {
        # W02 基线口径对账（93 条；含 40 条「白屏」类噪声，故不用它做归档分析）
        "baseline_filter": button_sql,
        "baseline_count": len(baseline_items),
        "baseline_with_causes": sum(1 for it in baseline_items if it["has_causes"]),
        "baseline_whitescreen": baseline_whitescreen,
        "baseline_pair_sim_stats": baseline_pair_stats,
        # 分析口径：按钮配色规则（46 条）
        "analysis_filter": button_analysis_sql,
        "keywords": list(DEFAULT_BUTTON_KEYWORDS),
        "keyword_counts": kw_counts,
        "count": len(button_items),
        "l2_rows": sum(1 for it in button_items if it["has_l2"]),
        "with_causes": len(keep_candidates),
        "pair_sim_stats": pair_stats,
        "cluster_stats": cluster_stats,
        "strict_cluster_count": len(strict_clusters),
        "groups": review_payload["groups"],
        "recommend_summary": {
            "keep_candidates_with_causes": len(keep_candidates),
            "bare_candidates_without_causes": len(bare_candidates),
            "auto_archive_candidates": None,
        },
        "keep_candidates": [
            {
                "memory_id": it["memory_id"],
                "length": it["length"],
                "causes_count": it["causes_count"],
                "content": it["content"][:120],
            }
            for it in sorted(keep_candidates, key=lambda it: (-it["length"], it["memory_id"]))
        ],
        "bare_candidates_sample": [
            {
                "memory_id": it["memory_id"],
                "length": it["length"],
                "content": it["content"][:120],
            }
            for it in sorted(bare_candidates, key=lambda it: (-it["length"], it["memory_id"]))[:20]
        ],
        "note": (
            "W02 的 93 条口径（按钮 AND 白）会把「白屏」类 UI bug 复盘一起捞进来"
            "（实测 " + str(baseline_whitescreen) + " 条），不能当作按钮配色规则集；"
            "本分析改用配色词口径（" + str(len(button_items)) + " 条）。该批以**语义重复**为主"
            "（同一按钮规则多种措辞），词法 max_sim=" + str(pair_stats["max_sim"]) + "，≥"
            + str(gray_zone) + " 的近重复对 0 组 → 词法口径不足以自动归并/归档，"
            "只给「带 causes 保留候选」清单；弱分组（sim≥0.4）仅供人工复核参考。"
        ),
    }

    # ---- 全库近重复（blocking 近似） ----
    all_clusters, block_meta = _cluster_blocked(items, gray_zone)
    size_dist: dict[str, int] = {}
    for cl in all_clusters:
        key = str(len(cl))
        size_dist[key] = size_dist.get(key, 0) + 1
    ordered = sorted(all_clusters, key=lambda cl: min(it["memory_id"] for it in cl))
    rng = random.Random(seed)
    sample_n = min(sample_groups, len(ordered))
    sampled = rng.sample(ordered, sample_n) if sample_n else []
    sampled = _sort_clusters(sampled)
    whole_payload = _clusters_payload(sampled, threshold=threshold, gray_zone=gray_zone)
    whole_out: dict[str, Any] = {
        "method": "lexical_bigram_jaccard + bigram blocking（**词法近似，非向量口径**）",
        "threshold_high": threshold,
        "threshold_gray": gray_zone,
        "blocking": block_meta,
        "clusters_found": len(all_clusters),
        "size_distribution": dict(sorted(size_dist.items(), key=lambda kv: int(kv[0]))),
        "members_total": sum(len(cl) for cl in all_clusters),
        "sampling": {
            "method": "deterministic random sample of clusters（按最小 memory_id 排序后 seed 抽样）",
            "seed": seed,
            "clusters_total": len(all_clusters),
            "sampled": len(sampled),
        },
        **whole_payload,
    }

    return {
        "button_class": button_out,
        "whole_db": whole_out,
        "disclaimer": (
            "近重复为词法近似（bigram jaccard）聚类，用于给人工归档排序；"
            "不代表 near_dup 向量口径判定（生产 near_dup 用 bge-m3 向量，本 dry-run 零 embed）。"
            "建议保留项按「带 causes > 最长 > 最新」排序，**未执行任何归档/删除**；"
            "gray 档（0.85≤sim<0.92）与大簇（union-find 链式连通）必须人工复核。"
        ),
    }


def _sort_clusters(clusters: list[list[dict]]) -> list[list[dict]]:
    """簇排序：大小 desc → 最大 updated_at desc（稳定可复现）。"""
    return sorted(
        clusters,
        key=lambda cl: (-len(cl), max(it["updated_at"] for it in cl)),
    )


def _clusters_payload(clusters: list[list[dict]], *, threshold: float,
                      gray_zone: float, sampled_from: int | None = None) -> dict[str, Any]:
    groups = []
    high_groups = high_archive = gray_groups = gray_archive = 0
    for cl in clusters:
        cl_sorted = sorted(cl, key=lambda it: it["updated_at"], reverse=True)
        rec = recommend_keep(cl_sorted)
        max_sim = 0.0
        rep = bigrams(cl_sorted[0]["content"])
        for other in cl_sorted[1:]:
            max_sim = max(max_sim, jaccard(rep, bigrams(other["content"])))
        tier = "high" if max_sim >= threshold else "gray"
        if tier == "high":
            high_groups += 1
            high_archive += len(rec["archive_memory_ids"])
        else:
            gray_groups += 1
            gray_archive += len(rec["archive_memory_ids"])
        groups.append({
            "size": len(cl),
            "tier": tier,
            "max_sim": round(max_sim, 4),
            "keep": rec["keep_memory_id"],
            "keep_reason": rec["keep_reason"],
            "archive": rec["archive_memory_ids"],
            "members": [
                {
                    "memory_id": it["memory_id"],
                    "length": it["length"],
                    "has_causes": it["has_causes"],
                    "causes_count": it["causes_count"],
                    "updated_at": it["updated_at"],
                    "content": it["content"][:120],
                }
                for it in cl_sorted
            ],
        })
    out: dict[str, Any] = {
        "groups": groups,
        "recommend_summary": {
            "keep": len(groups),
            "archive_candidates": high_archive + gray_archive,
            "tier_split": {
                "high": {"groups": high_groups, "archive_candidates": high_archive},
                "gray": {"groups": gray_groups, "archive_candidates": gray_archive},
            },
        },
    }
    if sampled_from is not None:
        out["sampled_from_clusters"] = sampled_from
        out["sampled_groups"] = len(groups)
    return out


# ---------- 4. Markdown 报告 ----------

def render_markdown(report: dict[str, Any]) -> str:
    """把 JSON 报告渲染为 Markdown（人工评审用）。"""
    lines: list[str] = []
    add = lines.append
    add("# 存量上下文回填 dry-run 报告（W11-W12）")
    add("")
    add(f"> 生成时间：{report.get('generated_at', '')}　｜　数据库：`{report.get('db', {}).get('path', '')}`")
    add("> 只读分析（mode=ro + query_only），未执行任何写入/归档。")
    add("")
    b = report.get("baseline", {})
    doc = b.get("baseline_doc", {})
    add("## 0. 基线对账")
    add("")
    add("| 指标 | 本次实测 | W02 文档基线 |")
    add("|---|---|---|")
    add(f"| active 记忆 | {b.get('active')} | {doc.get('active')} |")
    add(f"| l2_facts_json 非空 | {b.get('l2_facts_json_non_empty')} | {doc.get('l2_facts_json_non_empty')} |")
    add(f"| 其中有 causes | {b.get('with_causes')} | {doc.get('with_causes')} |")
    add(f"| causes 总条数 | {b.get('causes_total')} | — |")
    add(f"| memory_edges 总数 | {b.get('edges_total')} | {doc.get('edges_total')} |")
    edges = b.get("edges_by_relation") or {}
    if edges:
        add("")
        add("边分布：" + "、".join(f"{k}={v}" for k, v in sorted(edges.items())))
    add("")

    causes = report.get("causes_mapping", {})
    add("## 1. 存量 causes 可映射率（W04 映射器 dry-run）")
    add("")
    add(f"- 含 l2_facts_json 记录：{causes.get('l2_rows')}；其中带 causes：{causes.get('rows_with_causes')}；causes 总数：{causes.get('causes_total')}")
    add("")
    add("| 口径 | 可映射 causes | 覆盖率 | 至少一条可映射的记录 | 记录覆盖率 |")
    add("|---|---|---|---|---|")
    names = {
        "unit_strict": "单元严格（仅主干 content）",
        "same_source": "同 source_ref（同文件/批次）",
        "global_exact": "全库精确同文本（上界）",
    }
    for lv in ("unit_strict", "same_source", "global_exact"):
        st = causes.get("levels", {}).get(lv) or {}
        add(f"| {names[lv]} | {st.get('mappable')} | {st.get('rate', 0) * 100:.2f}% "
            f"| {st.get('rows_with_any_mappable')} | {st.get('rows_rate', 0) * 100:.2f}% |")
    add("")
    add("失败原因（from 缺失 / to 缺失 / 两端缺失 / 歧义 / from==to 自环）：")
    add("")
    for lv in ("unit_strict", "same_source", "global_exact"):
        st = causes.get("levels", {}).get(lv) or {}
        add(f"- **{names[lv]}**：from_missing={st.get('from_missing')}、to_missing={st.get('to_missing')}、"
            f"both_missing={st.get('both_missing')}、歧义={st.get('from_ambiguous', 0) + st.get('to_ambiguous', 0)}、"
            f"self={st.get('self')}、same_id={st.get('same_id')}")
    samples = (causes.get("levels", {}).get("global_exact") or {}).get("samples_unmapped") or []
    if samples:
        add("")
        add("不可映射样例（全库口径）：")
        add("")
        for s in samples:
            add(f"- `{s['from']}` → `{s['to']}`")
    add("")

    para = causes.get("paraphrase") or {}
    if para:
        add("### 1.1 为什么映射不上：causes 文本 vs 记忆原文")
        add("")
        add(f"- 端点总数：{para.get('endpoints')}；**单元内精确命中**：{para.get('unit_exact_hits')}"
            f"（{para.get('unit_exact_rate', 0) * 100:.2f}%）；仅子串包含：{para.get('unit_contained_hits')}"
            f"（{para.get('unit_contained_rate', 0) * 100:.2f}%）")
        ss = para.get("same_source_best_sim") or {}
        add(f"- 同 source_ref 内最佳词法相似度分布（rows={ss.get('rows_evaluated')}，"
            f"其中超 scope 抽样={ss.get('rows_scope_sampled')}，未评估={ss.get('rows_not_evaluated')}）："
            f"{ss.get('distribution')}")
        add(f"- 词法模糊口径可映射对（两端都 ≥ 阈值，仅供参考不可 auto-apply）：{ss.get('fuzzy_mappable_pairs')}")
        add(f"- 说明：{para.get('note')}")
        add("")

    nd = report.get("near_dup", {})
    btn = nd.get("button_class", {})
    add("## 2. 按钮类重复清理建议（dry-run）")
    add("")
    add("### 2.0 W02 基线口径对账")
    add("")
    bps = btn.get("baseline_pair_sim_stats") or {}
    add(f"- W02 口径 `{btn.get('baseline_filter')}` → {btn.get('baseline_count')} 条"
        f"（带 causes {btn.get('baseline_with_causes')} 条）")
    add(f"- ⚠️ 其中 **{btn.get('baseline_whitescreen')} 条是「白屏」类 UI bug 复盘**（不是按钮配色规则）；"
        f"词法 max_sim={bps.get('max_sim')}、≥0.85 对 {bps.get('pair_counts', {}).get('ge_0_85')} "
        "→ 该 93 条口径不能直接当按钮规则集用")
    add("")
    add("### 2.1 按钮配色规则分析集")
    add("")
    add(f"- 分析口径：`{btn.get('analysis_filter')}` → {btn.get('count')} 条"
        f"（带 l2_facts_json {btn.get('l2_rows')} 条；**带 causes {btn.get('with_causes')} 条**）")
    add(f"- 关键词分项命中：{'、'.join(f'{k}={v}' for k, v in (btn.get('keyword_counts') or {}).items())}")
    ps = btn.get("pair_sim_stats") or {}
    add(f"- 词法相似度：{ps.get('pairs_total')} 对中 max_sim={ps.get('max_sim')}；"
        f"逐级近重复对数 {ps.get('pair_counts')}")
    add(f"- 各阈值簇数：{btn.get('cluster_stats')}")
    rec = btn.get("recommend_summary") or {}
    add(f"- 建议：保留优先候选（带 causes）{rec.get('keep_candidates_with_causes')} 条；"
        f"裸事实（无 causes）{rec.get('bare_candidates_without_causes')} 条——"
        f"自动归档候选：{rec.get('auto_archive_candidates')}（词法口径不足以自动归档，见 note）")
    add(f"- 说明：{btn.get('note')}")
    add("")
    keep = btn.get("keep_candidates") or []
    if keep:
        add("### 2.2 保留优先候选（带 causes，按长度降序，前 10 条）")
        add("")
        for m in keep[:10]:
            add(f"- ✅ `{m['memory_id']}` 字数={m['length']} causes={m['causes_count']}：{m['content'][:70]}")
        add("")
    weak = btn.get("groups") or []
    if weak:
        add(f"### 2.3 弱分组（词法 sim≥0.4，仅人工复核参考，共 {len(weak)} 组）")
        add("")
        for i, g in enumerate(weak[:10], 1):
            add(f"- 组 {i}（{g['size']} 条，max_sim={g['max_sim']}，建议保留 `{g['keep']}`）："
                + "；".join(f"`{m['memory_id'][:8]}`({m['length']}字,causes={m['has_causes']})"
                           for m in g["members"]))
        add("")

    wd = nd.get("whole_db", {})
    add("## 3. 全库近重复聚类抽样（dry-run）")
    add("")
    add(f"- 口径：{wd.get('method')}")
    add(f"- 阈值：{wd.get('threshold_high')}（high）/ {wd.get('threshold_gray')}（gray）")
    blk = wd.get("blocking") or {}
    add(f"- blocking：items={blk.get('items')}、候选对={blk.get('candidate_pairs')}、"
        f"相似边={blk.get('sim_edges')}、截断={blk.get('pairs_truncated')}")
    add(f"- 全库近重复簇：{wd.get('clusters_found')} 组（成员 {wd.get('members_total')} 条）")
    add(f"- 规模分布：{wd.get('size_distribution')}")
    samp = wd.get("sampling") or {}
    add(f"- 抽样：{samp.get('method')}；sampled={samp.get('sampled')}/{samp.get('clusters_total')}")
    wrec = wd.get("recommend_summary") or {}
    add(f"- 抽样建议：保留 {wrec.get('keep')} 条、归档候选 {wrec.get('archive_candidates')} 条"
        f"（tier_split={wrec.get('tier_split')}）")
    add("")
    for i, g in enumerate((wd.get("groups") or [])[:12], 1):
        add(f"### 全库簇 {i}（{g['size']} 条，tier={g['tier']}，max_sim={g['max_sim']}，保留 `{g['keep']}`：{g['keep_reason']}）")
        for m in g["members"][:6]:
            flag = "✅保留" if m["memory_id"] == g["keep"] else "🗄归档候选"
            add(f"- {flag} `{m['memory_id']}` 字数={m['length']} causes={m['has_causes']}：{m['content'][:60]}")
        if g["size"] > 6:
            add(f"- …（其余 {g['size'] - 6} 条见 JSON）")
        add("")
    add("## 4. 纪律与未执行项")
    add("")
    add("- 未写任何表/行：连接 mode=ro + PRAGMA query_only=1；脚本无 --apply 模式。")
    add(f"- {nd.get('disclaimer', '')}")
    add("- 存量单元边界不可精确还原（无 _l2_unit），causes 覆盖率按三级阶梯呈现；"
        "是否回填、用哪级口径需 lead/用户决策。")
    add("")
    return "\n".join(lines)


# ---------- 5. CLI ----------

def _build_report(conn: sqlite3.Connection, *, db_path: str, mode: str,
                  sample_groups: int) -> dict[str, Any]:
    report: dict[str, Any] = {
        "generated_at": _now_iso(),
        "db": {"path": db_path, "mode": "readonly"},
        "baseline": analyze_baseline(conn),
    }
    if mode in ("all", "causes"):
        report["causes_mapping"] = analyze_causes(conn)
    if mode in ("all", "near-dup"):
        report["near_dup"] = analyze_near_dup(conn, sample_groups=sample_groups)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="W11-W12 存量上下文回填 dry-run（只读；绝不写库/不 apply）")
    parser.add_argument("--db", help="memory.db 或 .bak 快照路径（只读打开）")
    parser.add_argument("--mode", choices=["all", "causes", "near-dup"], default="all")
    parser.add_argument("--sample-groups", type=int, default=100,
                        help="全库近重复抽样组数（默认 100）")
    parser.add_argument("--json-out", help="把 JSON 报告写到该路径（UTF-8）")
    parser.add_argument("--stdout-json", action="store_true",
                        help="把 JSON 报告打到 stdout（ASCII 转义，跨机编码安全）")
    parser.add_argument("--report-out", help="把 Markdown 报告写到该路径")
    parser.add_argument("--render-json", help="只渲染：读该 JSON 报告生成 Markdown")
    args = parser.parse_args(argv)

    if args.render_json:
        report = json.loads(Path(args.render_json).read_text(encoding="utf-8"))
        md = render_markdown(report)
        if args.report_out:
            Path(args.report_out).write_text(md, encoding="utf-8")
            print(f"报告已写入: {args.report_out}")
        else:
            print(md)
        return 0

    if not args.db:
        parser.error("需要 --db（或使用 --render-json 渲染既有报告）")

    conn = open_db_readonly(args.db)
    try:
        report = _build_report(conn, db_path=args.db, mode=args.mode,
                               sample_groups=args.sample_groups)
    finally:
        conn.close()

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入: {args.json_out}")
    if args.report_out:
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(render_markdown(report), encoding="utf-8")
        print(f"报告已写入: {args.report_out}")
    if args.stdout_json:
        print(json.dumps(report, ensure_ascii=True))
    elif not args.json_out and not args.report_out:
        print(json.dumps(report, ensure_ascii=True, indent=2))

    # 摘要（人读）
    b = report["baseline"]
    print(f"[dry-run] active={b['active']} l2={b['l2_facts_json_non_empty']} "
          f"with_causes={b['with_causes']} causes={b['causes_total']} "
          f"edges={b['edges_total']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
