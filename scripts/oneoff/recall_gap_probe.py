# -*- coding: utf-8 -*-
"""候选池召回核实探针：近似重复入库的真实规模（只读副本库）。

问题：LLM 判 store 的新记忆中，有多少与库内已有记忆高相似（cos>=0.85）？
若规模可观 → 候选池漏召回实锤，近似查重价值确认。

方法：近 90 天全 store 批 + 混合批中被 store 的记忆（时间窗对齐），
与「其对齐时刻已存在」的库内记忆算 bigram cos（排除自比、排除同批新建）。
"""
import sqlite3
import json
import math
import random
from datetime import datetime, timedelta
from collections import Counter
import bisect

BASE = r"<副本库目录>"  # 本机只读副本目录（原 %TEMP%\sgme_verify；已按数据卫生脱敏，复跑时改为实际路径）
DB = BASE + r"\memory.db"


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace(" ", "T").replace("Z", "+00:00"))
    except Exception:
        return None


def bigrams(s):
    s = (s or "").lower()
    s = "".join(ch for ch in s if not ch.isspace())
    return Counter(s[i:i+2] for i in range(len(s)-1)) if len(s) > 1 else Counter()


def cos_bg(c1, c2):
    if not c1 or not c2:
        return 0.0
    common = set(c1) & set(c2)
    dot = sum(c1[t]*c2[t] for t in common)
    n1 = math.sqrt(sum(v*v for v in c1.values()))
    n2 = math.sqrt(sum(v*v for v in c2.values()))
    return dot/(n1*n2) if n1 and n2 else 0.0


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # 1. 全部含 store 的裁决批（近 90 天：2026-07-11 起）
    cur.execute("""select run_id, finished_at, action_counts from refine_runs
                   where stage='l1_conflict' and status='ok'
                   and action_counts is not null and action_counts != ''
                   and finished_at >= '2026-07-11'
                   order by finished_at""")
    batches = []
    for run_id, fin, ac in cur.fetchall():
        try:
            d = json.loads(ac)
        except Exception:
            continue
        n_store = d.get("store", 0) + d.get("create", 0)
        if n_store > 0:
            batches.append((run_id, parse_ts(fin), n_store))
    print(f"含 store 批（近90天）: {len(batches)}")

    # 2. 库内 active 记忆按时间排序
    cur.execute("select memory_id, content, created_at from memories where status='active' and content is not null")
    mems = [(m, c, parse_ts(t)) for m, c, t in cur.fetchall()]
    mems = [(m, c, t) for m, c, t in mems if t]
    mems.sort(key=lambda x: x[2])
    ts_list = [t for _, _, t in mems]

    # 3. 对齐：每批 store 数量 → 取批时刻前已存在、且属于「该批时间窗内新建」的后续记忆为待检样本
    #    更简单可靠：直接取全库 active 记忆抽样，计算「与其之前已存在的记忆」的最高 cos
    #    （近似重复入库的充分条件：新记忆入库时刻，库内已存在 cos>=0.85 的更早记忆）
    random.seed(11)
    sample = random.sample(mems, min(400, len(mems)))
    sample.sort(key=lambda x: x[2])

    dup_hits = 0
    checked = 0
    cases = []
    bg_cache = {}
    for mid, content, t in sample:
        lo = bisect.bisect_left(ts_list, t - timedelta(days=90))
        hi = bisect.bisect_left(ts_list, t)  # 严格早于该记忆创建
        prior = mems[lo:hi]
        if not prior:
            continue
        bg = bg_cache.setdefault(mid, bigrams(content))
        best_s, best_m = 0.0, None
        for pm, pc, _ in prior:
            if pm == mid:
                continue
            pb = bg_cache.setdefault(pm, bigrams(pc[:600]))
            s = cos_bg(bg, pb)
            if s > best_s:
                best_s, best_m = s, (pm, pc)
        checked += 1
        if best_s >= 0.85:
            dup_hits += 1
            if len(cases) < 6:
                cases.append((round(best_s, 3), content[:60], best_m[1][:60]))
    print(f"抽样核查: {checked} 条 | 其中 cos>=0.85 命中: {dup_hits} ({dup_hits*100//max(checked,1)}%)")
    print("--- 高相似入库案例（新 | 库内已有）---")
    for c in cases:
        print(c)
    con.close()


if __name__ == "__main__":
    main()
