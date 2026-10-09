# -*- coding: utf-8 -*-
"""重放第三段：全 store 批新记忆的 max-sim 分布（规则出手面验证）。

逻辑：全 store 批（action_counts 只有 store）= LLM 判「候选全不相关」。
若这些新记忆与库内 active 记忆的 bigram max-sim 集中在低区（<0.3），
则规则「max_sim < 阈值 → 短路 store」可在这些批出手，一致率逼近 100%。
"""
import sqlite3
import json
import math
import random
from collections import Counter

BASE = r"<副本库目录>"  # 本机只读副本目录（原 %TEMP%\sgme_verify；已按数据卫生脱敏，复跑时改为实际路径）
DB = BASE + r"\memory.db"


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

    # 1. 找全 store 批：action_counts 只有 store/create
    cur.execute("""select run_id, finished_at, action_counts, prompt_tokens
                   from refine_runs where stage='l1_conflict' and status='ok'
                   and action_counts is not null and action_counts != ''
                   order by finished_at desc limit 200""")
    cand_batches = []
    for run_id, fin, ac, tok in cur.fetchall():
        try:
            d = json.loads(ac)
        except Exception:
            continue
        keys = {k for k in d if k in ("store", "skip", "update", "merge", "create")}
        if keys in ({"store"}, {"create"}):
            cand_batches.append((run_id, fin, sum(d.values()), tok))
    print(f"全 store 批（近 200 run 内）: {len(cand_batches)}")

    # 2. 按时间窗对齐找新记忆（finished_at 前 120 秒内 created）
    stored_new = []
    for run_id, fin, cnt, tok in cand_batches[:60]:
        cur.execute("""
            select memory_id, content from memories
            where status='active' and created_at <= ? and created_at >= datetime(?, '-120 seconds')
            order by created_at desc limit ?
        """, (fin, fin, cnt + 3))
        rows = cur.fetchall()
        # 时间格式可能带 T/Z，宽松取前 cnt 条
        stored_new.extend(rows[:cnt])
    stored_new = [(m, c) for m, c in stored_new if c]
    print(f"对齐到的新记忆: {len(stored_new)}")
    random.seed(7)
    sample = random.sample(stored_new, min(150, len(stored_new)))

    # 3. 载全库 active 内容（仅前缀 400 字，控时长）
    cur.execute("select memory_id, substr(content,1,400) from memories where status='active'")
    pool = [(m, c) for m, c in cur.fetchall() if c]
    pool_bgs = [(m, bigrams(c)) for m, c in pool]
    print(f"对照池: {len(pool_bgs)} 条")

    maxsims = []
    for mid, content in sample:
        bg = bigrams(content)
        ms = max((cos_bg(bg, pb) for _, pb in pool_bgs), default=0.0)
        maxsims.append(ms)
    maxsims.sort()
    n = len(maxsims)

    def pct(q):
        return round(maxsims[min(int(n*q/100), n-1)], 3)

    print(f"全store批新记忆 max-sim 分位数 50/75/90/95/99: {[pct(q) for q in [50,75,90,95,99]]}")
    for th in [0.2, 0.3, 0.4]:
        below = sum(1 for s in maxsims if s < th)
        print(f"max-sim<{th}: {below}/{n} ({below*100//n}%) → 这些新记忆可被规则短路 store")
    con.close()


if __name__ == "__main__":
    main()
