#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1.5 P1 副本重放（T1.9）：向量口径近似查重首测——拦截率 / 灰区率 / 分数分布。

用途
----
P1 验收④：在「生产库快照的本地只读副本」上重放近似查重（F1）的向量口径判定。
主测量给出 hit（best>=0.92）/ gray（0.85<=best<0.92）/ pass 三分类首测基数；
另加分数分位 / 直方图 / 阈值敏感性 / 拦截案例清单 / 与生产 vector_search 的
交叉校验。与验证阶段 bigram 口径参照（4.5%）的对照差异属两套分，不可直比
（见《L15-P1副本重放首测记录》口径注）。

运行（在仓库根执行；交叉校验段需要 import sgme）
--------------------------------------------------
    python scripts/oneoff/l15_near_dup_replay.py --db <副本库路径> [--limit 400] [--seed 11] [--out <输出文件>]

方法
----
1. 样本：复刻验证阶段口径——active + content 非空 + created_at 可解析 → 按
   created_at 排序（稳定序）→ random.seed(seed) → sample(min(limit, N))。
2. 主测量（精确口径，零网络）：预载全部「带向量 active 记忆」的
   (memory_id, created_at, content, 向量矩阵 float32 N×D)。每条样本以其自身
   memory_vectors.embedding 作查询向量（不调 embed 端点，只用库存向量）；
   候选遮罩 = created_at ∈ [t-90d, t)（prior-only，复刻验证口径；ts<t 自然
   排除自身——样本自身 created_at==t）；余弦相似与生产 numpy 降级路径
   （sgme/data/search/vector.py::_numpy_cosine_search）同一公式
   dot/(|a||b|) 后 clip[0,1]；取最大分类：hit=best>=0.92 / gray=0.85<=best<0.92
   / pass。样本无向量 → 跳过并计数。范数预载一次，逐样本向量化遮罩取最大。
3. 交叉校验（默认 50 条等距子集）：用工作树 sgme.data.search.vector.vector_search
   (conn, vec, limit=200) 复算，按同规则（非自身 + prior 90d 窗）取 best 与
   主测量对比；因 top-200 被后来记忆挤占导致的差异归为方法差异并单列。
4. 副本库以 ``file:...?mode=ro`` URI 只读打开，全程仅 SELECT；绝不写库。

输出
----
- console：人类可读报告（指标 / 分位 / 直方图 / 敏感性 / 交叉校验 / 案例 top20）。
- --out <file>：完整 raw（含全量案例清单 + 逐样本表 + 交叉校验明细）写入该文件
  （父目录自动创建）；另在同路径输出 <file>.cases.tsv（hit/灰区全量案例清单，
  供报告附录与观察期分层抽样复用）。未给 --out 时全部打到 stdout。
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

#: 判定阈值：与 sgme.engine.l15.DEFAULT_NEAR_DUP 出厂保守值同步（快照口径）。
HIT_THRESHOLD = 0.92   # score >= 0.92 → hit（写隔离表）
GRAY_ZONE = 0.85       # 0.85 <= score < 0.92 → gray（放行 + 聚合计数）
WINDOW_DAYS = 90       # prior-only 候选窗（复刻验证阶段口径）

SENSITIVITY_THRESHOLDS = (0.80, 0.85, 0.90, 0.92, 0.95)
CROSSCHECK_N = 50          # 交叉校验子集大小（等距取样）
CROSSCHECK_LIMIT = 200     # vector_search top-K


# ---------------------------------------------------------------- 基础工具

def parse_ts(s: str | None) -> datetime | None:
    """解析 memory.created_at（ISO 变体容错；复刻验证脚本口径）。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace(" ", "T").replace("Z", "+00:00"))
    except Exception:
        return None


def one_line(s: str | None, n: int = 80) -> str:
    """内容摘要：压平空白 + 截断至 n 字（报告/TSV 数据卫生口径）。"""
    return " ".join((s or "").split())[:n]


def open_readonly(db_path: str) -> sqlite3.Connection:
    """以 file:...?mode=ro URI 只读打开副本库（全程仅 SELECT）。

    row_factory=sqlite3.Row 对齐生产 connect_memory 的连接形态——
    vector_search 按列名取行（r["embedding"]/r["distance"]），
    行工厂缺失会使检索静默返回空集（名取失败 → 行被跳过）。
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def quantile(sorted_vals: list[float], q: float) -> float | None:
    """最近秩分位（复刻验证脚本口径：idx=min(int(n*q/100), n-1)）。"""
    n = len(sorted_vals)
    if n == 0:
        return None
    return sorted_vals[min(int(n * q / 100), n - 1)]


def cls_of(best: float | None) -> str:
    """三分类：hit / gray / pass（None → empty（窗口内无候选））。"""
    if best is None:
        return "empty"
    if best >= HIT_THRESHOLD:
        return "hit"
    if best >= GRAY_ZONE:
        return "gray"
    return "pass"


# ---------------------------------------------------------------- 数据装载

def load_memories(con: sqlite3.Connection):
    """active + content 非空 的全量记忆（样本池），按 created_at 稳定排序。"""
    rows = con.execute(
        "select memory_id, content, created_at from memories "
        "where status='active' and content is not null"
    ).fetchall()
    mems = [(m, c, parse_ts(t)) for m, c, t in rows]
    mems = [(m, c, t) for m, c, t in mems if t]
    mems.sort(key=lambda x: x[2])
    return mems


def draw_sample(mems: list, limit: int, seed: int) -> list:
    """复刻验证阶段抽样口径：seed → sample(min(limit, N)) → 按时间排序。"""
    random.seed(seed)
    sample = random.sample(mems, min(limit, len(mems)))
    sample.sort(key=lambda x: x[2])
    return sample


def load_vectors(con: sqlite3.Connection):
    """预载全部「带向量 active 记忆」。

    返回 (ids, ts_arr, mat, norms, keep_index, skipped)：
      - ids: memory_id 列表（按 created_at 升序，并列时保持库内序）
      - ts_arr: float64 POSIX 秒数组（掩码用）
      - mat: float32 N×D 矩阵；norms: float32 N 范数
      - keep_index: memory_id → 矩阵行号
      - skipped: 结构跳过计数（维度不符 / 时间不可解析）
    """
    rows = con.execute(
        """
        select v.memory_id, v.embedding, v.dims, m.created_at
        from memory_vectors v
        join memories m on m.memory_id = v.memory_id
        where m.status='active'
        order by m.created_at asc
        """
    ).fetchall()

    parsed = []          # (mid, t, np.ndarray)
    skipped_bad_dims = 0
    skipped_bad_ts = 0
    dims_seen: dict[int, int] = {}
    for mid, blob, dims, ts in rows:
        arr = np.frombuffer(blob, dtype=np.float32)
        if dims <= 0 or arr.shape[0] != int(dims):
            skipped_bad_dims += 1
            continue
        t = parse_ts(ts)
        if t is None:
            skipped_bad_ts += 1
            continue
        dims_seen[arr.shape[0]] = dims_seen.get(arr.shape[0], 0) + 1
        parsed.append((mid, t, arr))

    # 维度一致性：多维度混仓时只保留众数维度（生产 numpy 路径也是按行跳维度不符）
    skipped_mixed_dims = 0
    if len(dims_seen) > 1:
        dominant = max(dims_seen, key=lambda k: dims_seen[k])
        kept = []
        for mid, t, arr in parsed:
            if arr.shape[0] == dominant:
                kept.append((mid, t, arr))
            else:
                skipped_mixed_dims += 1
        parsed = kept

    ids = [mid for mid, _, _ in parsed]
    ts_arr = np.array([t.timestamp() for _, t, _ in parsed], dtype=np.float64)
    mat = np.stack([arr for _, _, arr in parsed]) if parsed else np.zeros((0, 0), dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1) if parsed else np.zeros(0, dtype=np.float32)
    keep_index = {mid: i for i, mid in enumerate(ids)}

    dims = mat.shape[1] if parsed else 0
    skipped = {
        "bad_dims": skipped_bad_dims,
        "bad_ts": skipped_bad_ts,
        "mixed_dims": skipped_mixed_dims,
        "dims": dims,
        "uniform": len(dims_seen) <= 1,
    }
    return ids, ts_arr, mat, norms, keep_index, skipped


# ---------------------------------------------------------------- 主测量

def measure_sample(pos, mid, content, t, keep_index, mat, norms, ts_arr, cand_ids):
    """单条样本的主测量（返回记录 dict）。"""
    idx = keep_index.get(mid)
    if idx is None:
        return {"pos": pos, "mid": mid, "content": content, "t": t,
                "cls": "skip_no_vec", "best": None, "matched_id": None, "vec_idx": None}

    q = mat[idx]
    q_norm = np.float32(np.linalg.norm(q))
    if q_norm == 0:
        return {"pos": pos, "mid": mid, "content": content, "t": t,
                "cls": "skip_zero_vec", "best": None, "matched_id": None, "vec_idx": idx}

    t_s = t.timestamp()
    lo_s = t_s - WINDOW_DAYS * 86400.0
    mask = (ts_arr >= lo_s) & (ts_arr < t_s)   # ts<t 自然排除自身（自身 ts==t）
    if not mask.any():
        return {"pos": pos, "mid": mid, "content": content, "t": t,
                "cls": "empty", "best": None, "matched_id": None, "vec_idx": idx}

    # 余弦：dot/(|a|*|b|) 后 clip[0,1]（与生产 numpy 降级路径同式；float32）
    sims = (mat @ q) / (norms * q_norm)
    sims = np.clip(sims, 0.0, 1.0)
    masked = np.where(mask, sims, np.float32(-1.0))
    best_idx = int(masked.argmax())
    best = float(masked[best_idx])
    if best < 0.0:  # 理论不可达（mask 已判非空）；防御
        return {"pos": pos, "mid": mid, "content": content, "t": t,
                "cls": "empty", "best": None, "matched_id": None, "vec_idx": idx}
    return {"pos": pos, "mid": mid, "content": content, "t": t,
            "cls": cls_of(best), "best": best, "matched_id": cand_ids[best_idx],
            "vec_idx": idx}


# ---------------------------------------------------------------- 交叉校验

def run_crosscheck(con, subset, mat, keep_index, ts_by_id, cand_id_set):
    """50 条子集：vector_search(limit=200) 复算并按同规则取 best，与主测量对比。

    主测量无候选但 top-200 内截断等差异分别归因：
      - truncated：|diff|>1e-3 且主测量 matched_id 不在 top-200 返回集（被挤占）；
      - backend_drift：其余 |diff|>1e-3（后端数值差异）；
      - matched_id 分歧（分数近乎相等、不同命中对象）单独计数。
    """
    try:
        import sgme.data.search.vector as vector_mod  # noqa: PLC0415（延迟导入：仅此段需要）
    except Exception as e:  # pragma: no cover - 运行环境问题须显式暴露
        print(f"[FATAL] 交叉校验需要可 import 的 sgme（PYTHONPATH=仓库根）：{e}")
        sys.exit(2)

    records = []
    for rec in subset:
        mid = rec["mid"]
        q = mat[rec["vec_idx"]]
        q_list = [float(x) for x in q]
        try:
            rows = vector_mod.vector_search(con, q_list, limit=CROSSCHECK_LIMIT)
        except Exception as e:
            records.append({"mid": mid, "error": str(e)})
            continue
        t = rec["t"]
        t_s = t.timestamp()
        lo_s = t_s - WINDOW_DAYS * 86400.0
        top_ids = {r["memory_id"] for r in rows}
        best_xc, matched_xc = None, None
        for r in rows:
            cid = r["memory_id"]
            if cid == mid:
                continue
            cts = ts_by_id.get(cid)
            if cts is None:
                continue
            c_s = cts.timestamp()
            if not (lo_s <= c_s < t_s):
                continue
            s = float(r["score"])
            if best_xc is None or s > best_xc:
                best_xc, matched_xc = s, cid
        main_best = rec["best"]
        diff = None
        reason = ""
        if main_best is None and best_xc is None:
            reason = "both_none"
        elif main_best is None or best_xc is None:
            reason = "missing_side"
        else:
            diff = abs(float(main_best) - float(best_xc))
            if diff > 1e-3:
                if rec["matched_id"] not in top_ids:
                    reason = "truncated"
                else:
                    reason = "backend_drift"
        records.append({
            "mid": mid,
            "main_best": main_best,
            "xc_best": best_xc,
            "diff": diff,
            "main_cls": rec["cls"],
            "xc_cls": cls_of(best_xc),
            "main_matched": rec["matched_id"],
            "xc_matched": matched_xc,
            "reason": reason,
            "top_ids": top_ids,
        })

    # ST-58/T-290：全局 `_VEC_EXTENSION_LOADED` 语义已收窄为「包是否可用」，
    # 不再代表本连接的实际后端；改读连接级判定结果。
    backend = "sqlite-vec" if vector_mod.try_load_vec_extension(con) else "numpy"
    diffs = [r["diff"] for r in records if r.get("diff") is not None]
    stats = {
        "n": len(records),
        "backend": backend,
        "errors": sum(1 for r in records if "error" in r),
        "both_none": sum(1 for r in records if r.get("reason") == "both_none"),
        "missing_side": sum(1 for r in records if r.get("reason") == "missing_side"),
        "max_abs_diff": max(diffs) if diffs else None,
        "mean_abs_diff": (sum(diffs) / len(diffs)) if diffs else None,
        "gt_1e-3": sum(1 for d in diffs if d > 1e-3),
        "truncated": sum(1 for r in records if r.get("reason") == "truncated"),
        "backend_drift": sum(1 for r in records if r.get("reason") == "backend_drift"),
        "matched_id_diff": sum(
            1 for r in records
            if r.get("diff") is not None and r.get("main_matched") != r.get("xc_matched")
        ),
        "class_flips": [
            r for r in records
            if r.get("diff") is not None and r.get("main_cls") != r.get("xc_cls")
        ],
    }
    return stats, records


# ---------------------------------------------------------------- 报告组装

def build_report(args, n_active, n_vec, skipped, mems, sample, measured,
                 cross_stats, cases):
    """组装人类可读 + raw 段落。返回 (human_lines, raw_lines)。"""
    H: list[str] = []
    R: list[str] = []

    def h(line: str = ""):
        H.append(line)

    def r(line: str = ""):
        R.append(line)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    h("==== L1.5 P1 副本重放（T1.9）｜向量口径近似查重首测 ====")
    h(f"运行时间: {now} ｜ limit={args.limit} seed={args.seed} ｜ 候选窗: prior-only {WINDOW_DAYS}d "
      f"｜ 阈值: hit>={HIT_THRESHOLD} / gray=[{GRAY_ZONE},{HIT_THRESHOLD})")
    h(f"[载入] active 记忆={n_active} ｜ 带向量={n_vec} ｜ 向量维度={skipped['dims']} "
      f"｜ uniform={skipped['uniform']} ｜ 结构跳过: dims={skipped['bad_dims']} ts={skipped['bad_ts']} "
      f"mixed={skipped['mixed_dims']}")
    h(f"[样本] 抽样={len(sample)}（池 {len(mems)}）｜ 主测量 n={len(measured['rows'])} "
      f"｜ 无向量跳过={measured['skipped_no_vec']} ｜ 零范数跳过={measured['skipped_zero_vec']} "
      f"｜ 空 prior 窗={measured['empty_prior']}")

    rows = measured["rows"]          # 全部样本逐条记录（含跳过）
    vals = sorted(x["best"] for x in rows if x["best"] is not None)
    n_m = len(vals)
    n_hit = sum(1 for x in rows if x["cls"] == "hit")
    n_gray = sum(1 for x in rows if x["cls"] == "gray")
    n_pass = sum(1 for x in rows if x["cls"] == "pass")
    n_sample = len(sample)

    def pct(a, b):
        return f"{a}/{b} ({a * 100.0 / b:.2f}%)" if b else f"{a}/0 (n/a)"

    h("")
    h(f"[首测基数．可测 {n_m}] hit={pct(n_hit, n_m)} ｜ gray={pct(n_gray, n_m)} ｜ pass={pct(n_pass, n_m)}")
    h(f"[首测基数．折算全样本 {n_sample}] hit={pct(n_hit, n_sample)} ｜ gray={pct(n_gray, n_sample)}"
      f" ｜ 两口径对照说明见首测记录（bigram 参照 18/400=4.50%，两套分不可直比）")

    if vals:
        mean_v = sum(vals) / len(vals)
        h(f"[最大相似分] 均值={mean_v:.4f} ｜ p50={quantile(vals, 50):.4f} p75={quantile(vals, 75):.4f} "
          f"p90={quantile(vals, 90):.4f} p95={quantile(vals, 95):.4f} p99={quantile(vals, 99):.4f}")
        h("[直方图 0.10 步长]")
        for i in range(10):
            lo, hi = i / 10.0, (i + 1) / 10.0
            if i < 9:
                cnt = sum(1 for v in vals if lo <= v < hi)
            else:
                cnt = sum(1 for v in vals if lo <= v <= hi)
            bar = "#" * min(cnt, 60)
            h(f"  [{lo:.1f},{hi:.1f}) {cnt:4d} |{bar}")
        h("[高位细查 0.02 步长，[0.80,1.00]]")
        for i in range(10):
            lo, hi = 0.80 + i * 0.02, 0.80 + (i + 1) * 0.02
            if i < 9:
                cnt = sum(1 for v in vals if lo <= v < hi)
            else:
                cnt = sum(1 for v in vals if lo <= v <= hi)
            h(f"  [{lo:.2f},{hi:.2f}) {cnt:4d}")
        h("[阈值敏感性（best>=th 计数，分母=可测）]")
        for th in SENSITIVITY_THRESHOLDS:
            cnt = sum(1 for v in vals if v >= th)
            h(f"  th={th:.2f}: {pct(cnt, n_m)}")

    h("")
    if cross_stats:
        cs = cross_stats
        mx = cs["max_abs_diff"]
        mn = cs["mean_abs_diff"]
        h(f"[交叉校验] 子集={cs['n']} ｜ 后端={cs['backend']} ｜ 异常={cs['errors']} "
          f"｜ 双侧无候选={cs['both_none']}")
        h(f"  max|diff|={'n/a' if mx is None else f'{mx:.3e}'} ｜ "
          f"mean|diff|={'n/a' if mn is None else f'{mn:.3e}'} ｜ >1e-3={cs['gt_1e-3']} "
          f"｜ 截断归因={cs['truncated']} ｜ 其余数值差={cs['backend_drift']} "
          f"｜ 单侧缺失={cs['missing_side']} ｜ matched_id 分歧={cs['matched_id_diff']} "
          f"｜ 类翻转={len(cs['class_flips'])}")

    n_hit_cases = sum(1 for c in cases if c["cls"] == "hit")
    n_gray_cases = sum(1 for c in cases if c["cls"] == "gray")
    h(f"[案例] hit={n_hit_cases} 条 ｜ gray={n_gray_cases} 条（清单按分数降序；TSV=全量，附录=top50，console=top20）")
    h("")
    h("--- hit/gray 案例（top20，score / 样本摘要 vs 命中摘要 / 双方 id）---")
    for c in cases[:20]:
        h(f"  [{c['cls']}] {c['score']:.4f} | {c['sample_id']} :: {c['sample_summary']} || "
          f"{c['matched_id']} :: {c['matched_summary']}")

    # ---- raw 附录 ----
    r("")
    r("==== RAW 附录 ====")
    r(f"[meta] db={args.db} (ro) limit={args.limit} seed={args.seed} window_days={WINDOW_DAYS} "
      f"hit>= {HIT_THRESHOLD} gray>={GRAY_ZONE}")
    r("")
    r("--- 全量案例清单（hit+gray，score 降序）---")
    r("rank\tclass\tscore\tsample_id\tsample_created_at\tmatched_id\tmatched_created_at\t"
      "sample_summary\tmatched_summary")
    for i, c in enumerate(cases, start=1):
        r(f"{i}\t{c['cls']}\t{c['score']:.6f}\t{c['sample_id']}\t{c['sample_created_at']}\t"
          f"{c['matched_id']}\t{c['matched_created_at']}\t{c['sample_summary']}\t{c['matched_summary']}")
    r("")
    r("--- 逐样本主测量表（全部抽样）---")
    r("pos\tmemory_id\tcreated_at\tbest_score\tclass\tmatched_id")
    for x in rows:
        b = "NA" if x["best"] is None else f"{x['best']:.6f}"
        r(f"{x['pos']}\t{x['mid']}\t{x['t'].strftime('%Y-%m-%d %H:%M:%S')}\t{b}\t{x['cls']}\t"
          f"{x['matched_id'] or ''}")
    if cross_stats and cross_stats.get("records"):
        r("")
        r("--- 交叉校验明细（vector_search top-200 复算）---")
        r("memory_id\tmain_best\txc_best\tdiff\tmain_cls\txc_cls\tmain_matched\txc_matched\treason")
        for cr in cross_stats["records"]:
            if "error" in cr:
                r(f"{cr['mid']}\tERROR\t\t\t\t\t\t\t{cr['error']}")
                continue
            b1 = "NA" if cr["main_best"] is None else f"{cr['main_best']:.6f}"
            b2 = "NA" if cr["xc_best"] is None else f"{cr['xc_best']:.6f}"
            d = "NA" if cr["diff"] is None else f"{cr['diff']:.3e}"
            r(f"{cr['mid']}\t{b1}\t{b2}\t{d}\t{cr['main_cls']}\t{cr['xc_cls']}\t"
              f"{cr['main_matched'] or ''}\t{cr['xc_matched'] or ''}\t{cr['reason']}")
    return H, R


# ---------------------------------------------------------------- 主入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="L1.5 P1 副本重放（T1.9）：向量口径近似查重首测（只读副本库，零网络）"
    )
    ap.add_argument("--db", required=True, help="副本库路径（只读打开，仅 SELECT）")
    ap.add_argument("--limit", type=int, default=400, help="样本量（默认 400）")
    ap.add_argument("--seed", type=int, default=11, help="抽样随机种子（默认 11）")
    ap.add_argument("--out", default=None, help="完整 raw 输出文件（默认 stdout）")
    args = ap.parse_args(argv)

    if not Path(args.db).is_file():
        print(f"[FATAL] --db 不存在: {args.db}")
        return 2

    con = open_readonly(args.db)
    try:
        # 1) 记忆池与样本
        mems = load_memories(con)
        sample = draw_sample(mems, args.limit, args.seed)

        # 2) 向量预载
        cand_ids, ts_arr, mat, norms, keep_index, skipped = load_vectors(con)

        # 3) 主测量
        ts_by_id = {m: t for m, _, t in mems}          # active 全量 id→created_at
        content_by_id = {m: c for m, c, _ in mems}     # active 全量 id→content
        rows = []
        for pos, (mid, content, t) in enumerate(sample, start=1):
            rows.append(measure_sample(pos, mid, content, t, keep_index, mat, norms, ts_arr, cand_ids))
        measured = {
            "rows": rows,
            "skipped_no_vec": sum(1 for x in rows if x["cls"] == "skip_no_vec"),
            "skipped_zero_vec": sum(1 for x in rows if x["cls"] == "skip_zero_vec"),
            "empty_prior": sum(1 for x in rows if x["cls"] == "empty"),
        }

        # 4) 案例清单（hit+gray，按分数降序）
        cases = []
        for x in rows:
            if x["cls"] in ("hit", "gray") and x["best"] is not None and x["matched_id"]:
                m_ts = ts_by_id.get(x["matched_id"])
                cases.append({
                    "cls": x["cls"],
                    "score": x["best"],
                    "sample_id": x["mid"],
                    "sample_created_at": x["t"].strftime("%Y-%m-%d %H:%M:%S"),
                    "matched_id": x["matched_id"],
                    "matched_created_at": m_ts.strftime("%Y-%m-%d %H:%M:%S") if m_ts else "",
                    "sample_summary": one_line(x["content"], 80),
                    "matched_summary": one_line(content_by_id.get(x["matched_id"], ""), 80),
                })
        cases.sort(key=lambda c: (-c["score"], c["sample_id"]))

        # 5) 交叉校验（等距 50 条）
        measurable = [x for x in rows if x["best"] is not None or x["cls"] == "empty"]
        step = max(1, len(measurable) // CROSSCHECK_N)
        subset = measurable[::step][:CROSSCHECK_N]
        cross_stats = None
        if subset:
            stats, records = run_crosscheck(con, subset, mat, keep_index, ts_by_id, set(cand_ids))
            stats["records"] = records
            cross_stats = stats

        # 6) 报告
        n_active = len(mems)
        n_vec = len(cand_ids)
        H, R = build_report(args, n_active, n_vec, skipped, mems, sample, measured, cross_stats, cases)

        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(H + R) + "\n", encoding="utf-8")
            tsv_path = out_path.with_suffix(".cases.tsv")
            tsv_lines = [
                "rank\tclass\tscore\tsample_id\tsample_created_at\tmatched_id\tmatched_created_at\t"
                "sample_summary\tmatched_summary"
            ]
            for i, c in enumerate(cases, start=1):
                tsv_lines.append(
                    f"{i}\t{c['cls']}\t{c['score']:.6f}\t{c['sample_id']}\t{c['sample_created_at']}\t"
                    f"{c['matched_id']}\t{c['matched_created_at']}\t{c['sample_summary']}\t{c['matched_summary']}"
                )
            tsv_path.write_text("\n".join(tsv_lines) + "\n", encoding="utf-8")
            print("\n".join(H))
            print(f"[out] 完整 raw 已写入: {out_path}")
            print(f"[out] 案例 TSV 已写入: {tsv_path}")
        else:
            print("\n".join(H + R))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台兜底
        except Exception:
            pass
    raise SystemExit(main())
