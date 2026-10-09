# -*- coding: utf-8 -*-
"""重放第三段 v2：Python 侧时间对齐修正版。"""
import sqlite3
import json
import math
import random
from datetime import datetime, timedelta, timezone
from collections import Counter

BASE = r"<副本库目录>"  # 本机只读副本目录（原 %TEMP%\sgme_verify；已按数据卫生脱敏，复跑时改为实际路径）
DB = BASE + r"\memory.db"


def parse_ts(s):
    if not s:
        return None
    s = s.strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
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

    # 1. 全 store 批
    cur.execute("""select run_id, finished_at, action_counts from refine_runs
                   where stage='l1_conflict' and status='ok'
                   and action_counts is not null and action_counts != ''
                   order by finished_at desc limit 400""")
    batches = []
    for run_id, fin, ac in cur.fetchall():
        try:
            d = json.loads(ac)
        except Exception:
            continue
        keys = {k for k in d if k in ("store", "skip", "update", "merge", "create")}
        if keys in ({"store"}, {"create"}):
            batches.append((run_id, parse_ts(fin), sum(d.values())))
    print(f"全 store 批(近400 run): {len(batches)}")

    # 2. 时间对齐：新记忆 created_at ∈ [finished-180s, finished]
    stored_new = []
    cur.execute("select memory_id, content, created_at from memories where status='active' and content is not null")
    mems = cur.fetchall()
    mems_ts = [(m, c, parse_ts(t)) for m, c, t in mems]
    mems_ts = [(m, c, t) for m, c, t in mems_ts if t]
    mems_ts.sort(key=lambda x: x[2])
    import bisect
    ts_list = [t for _, _, t in mems_ts]
    for run_id, fin, cnt in batches:
        if fin is None:
            continue
        lo = bisect.bisect_left(ts_list, fin - timedelta(seconds=180))
        hi = bisect.bisect_right(ts_list, fin)
        window = mems_ts[lo:hi]
        stored_new.extend((m, c) for m, c, _ in window[:cnt])
    # 去重
    seen = set()
    uniq = []
    for m, c in stored_new:
        if m not in seen:
            seen.add(m)
            uniq.append((m, c))
    stored_new = uniq
    print(f"对齐到的新记忆(去重): {len(stored_new)}")
    if not stored_new:
        print("!! 对齐仍为空 — 打印 batch finished_at 样本与记忆时间范围诊断")
        cur.execute("select max(created_at) from memories")
        print("memories max created_at:", cur.fetchone())
        print("batch fin 样本:", [str(b[1]) for b in batches[:5]])
        return

    random.seed(7)
    sample = random.sample(stored_new, min(120, len(stored_new)))

    cur.execute("select memory_id, substr(content,1,400) from memories where status='active' and content is not null")
    pool_bgs = [(m, bigrams(c)) for m, c in cur.fetchall()]
    print(f"对照池: {len(pool_bgs)}")

    maxsims = []
    examples = []
    for mid, content in sample:
        bg = bigrams(content)
        best_s, best_m = 0.0, None
        for pm, pb in pool_bgs:
            s = cos_bg(bg, pb)
            if s > best_s:
                best_s, best_m = s, pm
        maxsims.append(best_s)
        if len(examples) < 3:
            best_content = next((c for m, c in pool_bgs if m == best_m), "")
            examples.append((round(best_s, 3), content[:60], best_content[:60]))
    maxsims.sort()
    n = len(maxsims)

    def pct(q):
        return round(maxsims[min(int(n*q/100), n-1)], 3)

    print(f"max-sim 分位数 50/75/90/95/99: {[pct(q) for q in [50,75,90,95,99]]}")
    for th in [0.2, 0.3, 0.4]:
        below = sum(1 for s in maxsims if s < th)
        print(f"max-sim<{th}: {below}/{n} ({below*100//n}%) 可短路 store")
    print("--- 样本（新内容 vs 其最高相似旧内容）---")
    for e in examples:
        print(e)
    con.close()


if __name__ == "__main__":
    main()
