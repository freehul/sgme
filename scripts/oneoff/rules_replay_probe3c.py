# -*- coding: utf-8 -*-
"""重放第三段 v3：排除自比。"""
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

    cur.execute("select memory_id, content, created_at from memories where status='active' and content is not null")
    mems = [(m, c, parse_ts(t)) for m, c, t in cur.fetchall()]
    mems = [(m, c, t) for m, c, t in mems if t]
    mems.sort(key=lambda x: x[2])
    ts_list = [t for _, _, t in mems]

    stored_new = []
    for run_id, fin, cnt in batches:
        if fin is None:
            continue
        lo = bisect.bisect_left(ts_list, fin - timedelta(seconds=180))
        hi = bisect.bisect_right(ts_list, fin)
        for m, c, _ in mems[lo:hi][:cnt]:
            stored_new.append((m, c))
    seen = set()
    stored_new = [x for x in stored_new if not (x[0] in seen or seen.add(x[0]))]
    print(f"对齐到的新记忆(去重): {len(stored_new)}")

    random.seed(7)
    sample = random.sample(stored_new, min(120, len(stored_new)))

    # 池：内容前 400 字 + bigram
    cur.execute("select memory_id, substr(content,1,400) from memories where status='active' and content is not null")
    pool = [(m, c, bigrams(c)) for m, c in cur.fetchall()]
    print(f"对照池: {len(pool)}")

    maxsims = []
    examples = []
    for mid, content in sample:
        bg = bigrams(content)
        best_s, best_c = 0.0, ""
        for pm, pc, pb in pool:
            if pm == mid:
                continue  # 排除自身
            s = cos_bg(bg, pb)
            if s > best_s:
                best_s, best_c = s, pc
        maxsims.append(best_s)
        if len(examples) < 5:
            examples.append((round(best_s, 3), content[:60], best_c[:60]))
    maxsims.sort()
    n = len(maxsims)

    def pct(q):
        return round(maxsims[min(int(n*q/100), n-1)], 3)

    print(f"max-sim 分位数 50/75/90/95/99: {[pct(q) for q in [50,75,90,95,99]]}")
    for th in [0.2, 0.3, 0.4]:
        below = sum(1 for s in maxsims if s < th)
        print(f"max-sim<{th}: {below}/{n} ({below*100//n}%) 可短路 store")
    print("--- 样本（新内容 vs 库内最高相似旧内容，已排除自比）---")
    for e in examples:
        print(e)
    con.close()


if __name__ == "__main__":
    main()
