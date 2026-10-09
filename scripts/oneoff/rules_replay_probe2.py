# -*- coding: utf-8 -*-
"""重放第二段：短路收益面 + 误杀风险面（只读副本库）。"""
import sqlite3
import math
import json
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

    # ---- A. 全 store 批占比（可短路收益面）----
    cur.execute("select action_counts, prompt_tokens from refine_runs where stage='l1_conflict' and status='ok' and action_counts is not null and action_counts != ''")
    all_store = all_store_tok = 0
    mixed = mixed_tok = 0
    no_llm = 0
    for (ac, tok) in cur.fetchall():
        try:
            d = json.loads(ac)
        except Exception:
            continue
        if not d:
            continue
        keys = {k for k in d if k in ("store", "skip", "update", "merge", "create")}
        if not keys:
            no_llm += 1
            continue
        if keys == {"store"} or keys == {"create"}:
            all_store += 1
            all_store_tok += tok or 0
        else:
            mixed += 1
            mixed_tok += tok or 0
    print(f"[A] l1_conflict 批: 全store批={all_store} 混合批={mixed} 非裁决批={no_llm}")
    print(f"    全store批 prompt tokens={all_store_tok:,} 混合批={mixed_tok:,}")

    # ---- B. 高相似未归档对（误杀风险面：cos>=0.85 的 active 对未被 supersede）----
    cur.execute("select memory_id, content from memories where status='active'")
    acts = cur.fetchall()
    print(f"[B] active memories: {len(acts)} — 抽样计算高相似对")
    # 抽 3000 条计算两两（抽样内）高相似对
    import random
    random.seed(42)
    sample = random.sample(acts, min(3000, len(acts)))
    bgs = [(mid, bigrams(c)) for mid, c in sample]
    high = 0
    pairs = []
    for i in range(len(bgs)):
        for j in range(i+1, len(bgs)):
            s = cos_bg(bgs[i][1], bgs[j][1])
            if s >= 0.85:
                high += 1
                if len(pairs) < 5:
                    pairs.append((round(s, 3), sample[i][1][:50], sample[j][1][:50]))
    print(f"    3000 条抽样内 cos>=0.85 对数: {high}")
    for p in pairs:
        print("   ", p)

    con.close()


if __name__ == "__main__":
    main()
