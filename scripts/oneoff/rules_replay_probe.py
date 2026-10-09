# -*- coding: utf-8 -*-
"""L1.5 规则化重放探针（只读副本库，零 LLM 调用，零写入）。

目的：验证「确定性规则层」能在多少真实 LLM 裁决事件上复现 LLM 的裁决。
真值来源：memory_archive.supersession 事件（旧记忆 archived_at + superseded_by = LLM
当年判 update/merge 的结果），是唯一带时间戳的裁决真值。

规则 R-sim（候选）：新记忆与被归档旧记忆的字符 2-gram 余弦相似度。
- 若相似度分布与「随机同维度旧记忆」显著可分 → 存在可用阈值
- 分位数 / 直方图给出「极低相似 → 短路 store」「极高相似 → 短路 skip」的候选阈值
"""
import sqlite3
import sys
import math
import json
from collections import Counter

BASE = r"<副本库目录>"  # 本机只读副本目录（原 %TEMP%\sgme_verify；已按数据卫生脱敏，复跑时改为实际路径）
DB = BASE + r"\memory.db"


def bigrams(s: str) -> Counter:
    s = (s or "").lower()
    s = "".join(ch for ch in s if not ch.isspace())
    return Counter(s[i : i + 2] for i in range(len(s) - 1)) if len(s) > 1 else Counter()


def cos_bg(c1: Counter, c2: Counter) -> float:
    if not c1 or not c2:
        return 0.0
    common = set(c1) & set(c2)
    dot = sum(c1[t] * c2[t] for t in common)
    n1 = math.sqrt(sum(v * v for v in c1.values()))
    n2 = math.sqrt(sum(v * v for v in c2.values()))
    return dot / (n1 * n2) if n1 and n2 else 0.0


def pct(sorted_list, q):
    n = len(sorted_list)
    return round(sorted_list[min(int(n * q / 100), n - 1)], 3)


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # ---- 真值事件：supersession（排除 TTL 清理）----
    cur.execute(
        """
        select a.memory_id old_id, a.content old_content, a.superseded_by new_id,
               m.content new_content, m.created_at new_created
        from memory_archive a join memories m on a.superseded_by = m.memory_id
        where a.superseded_by != 'dream_purge_expired'
        """
    )
    events = cur.fetchall()
    print(f"supersession events: {len(events)}")

    # ---- 规则 R-sim：新旧内容 bigram 余弦 ----
    sims = []
    for old_id, old_c, new_id, new_c, _ in events:
        sims.append(cos_bg(bigrams(old_c), bigrams(new_c)))
    sims.sort()
    n = len(sims)
    print("R-sim 相似度分位数 0/5/25/50/75/95/100:",
          [pct(sims, q) for q in [0, 5, 25, 50, 75, 95, 100]])

    # ---- 对照组：随机抽取「同库 active 记忆」与事件新记忆算相似度（负样本分布）----
    cur.execute("select memory_id, content from memories where status='active' order by random() limit 8000")
    negatives = cur.fetchall()
    neg_sims = []
    for i, (old_id, old_c, new_id, new_c, _) in enumerate(events[:4000]):
        neg_id, neg_c = negatives[i % len(negatives)]
        if neg_id == new_id:
            continue
        neg_sims.append(cos_bg(bigrams(old_c), bigrams(neg_c)))
    neg_sims.sort()
    print("负样本(随机对照)分位数 50/75/95/99:",
          [pct(neg_sims, q) for q in [50, 75, 95, 99]])

    # ---- 可分性:取几个阈值看正样本覆盖率(规则"能抓到多少真 update/merge") ----
    for th in [0.3, 0.5, 0.7]:
        cov = sum(1 for s in sims if s >= th)
        fp = sum(1 for s in neg_sims if s >= th)
        print(f"th={th}: 正样本覆盖 {cov}/{n} ({cov*100//n}%) | 负样本误触 {fp}/{len(neg_sims)} ({fp*100//max(len(neg_sims),1)}%)")

    # ---- 规则 R-exact-ish：新内容是否为旧内容的近似重复(高相似=该 skip) ----
    near_dup = sum(1 for s in sims if s >= 0.85)
    print(f"near-dup(cos>=0.85): {near_dup} ({near_dup*100//n}%) —— 这些本可由查重规则短路(部分已由 T-167 精确查重拦截)")

    con.close()
    json.dump(sims, open(BASE + r"\event_sims.json", "w"))


if __name__ == "__main__":
    main()
