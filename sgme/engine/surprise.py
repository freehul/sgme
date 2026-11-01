"""engine/surprise.py：惊讶度先验（设计决策 D-78）。

新记忆落库时，与同 memory_type 的已有记忆做词法比对，算出它有多「出乎意料」：
surprise = 1 - 最大 jaccard 相似度（0-1，越大越意外）。纯词法计算，零 LLM。

口径（D-78）：
- 比对池 = 同 memory_type 的最近 sample_limit 条 active 记忆（updated_at DESC）
- confidence：compared < 30 记 "low"（冷启动样本不足），否则 "normal"
- surprise = 1 - max(jaccard)；空比对池视为全新 → 1.0
- batch_backfill 只算分不写库（写库由后续落库链路决定），仅返回统计
"""

from __future__ import annotations

import re
import sqlite3

# 冷启动样本下限：少于该值时先验不可信，confidence="low"
CONFIDENCE_MIN_SAMPLES = 30

# 默认比对池大小：取最近 300 条（D-78）
SAMPLE_LIMIT = 300

# 英文/数字段：小写化后按空格切；中文段：滑窗 2-gram
_EN_RE = re.compile(r"[a-z0-9]+")
_ZH_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> set[str]:
    """分词：中文按 2-gram 切，英文按空格切小写，标点去除。

    - 中文连续段滑窗取 2-gram（「记忆引擎」→ {记忆, 忆引, 引擎}）；
      段长为 1 时退化为单字 token，避免整段信息丢失。
    - 英文/数字连续段整体小写成一个 token（「Hello」→ hello、「Python3」→ python3），
      即按空格自然切分；中英混排时各自成段。
    - 标点/空白/符号一律丢弃，不产生 token。
    """
    tokens: set[str] = set()
    if not text:
        return tokens
    for seg in _ZH_RE.findall(text):
        if len(seg) == 1:
            tokens.add(seg)
        else:
            tokens.update(seg[i:i + 2] for i in range(len(seg) - 1))
    tokens.update(m.group(0) for m in _EN_RE.finditer(text.lower()))
    return tokens


def jaccard(a: set[str], b: set[str]) -> float:
    """jaccard 相似度 = |交集| / |并集|；两者皆空返回 0.0（空内容不视作相同）。"""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _fetch_sample(
    conn: sqlite3.Connection,
    memory_type: str | None,
    sample_limit: int,
) -> list[dict]:
    """取比对池：同 memory_type（None=不限）的最近 sample_limit 条 active 记忆。"""
    sql = (
        "SELECT memory_id, content FROM memories "
        "WHERE status='active'"
    )
    params: list = []
    if memory_type is not None:
        sql += " AND memory_type=?"
        params.append(memory_type)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(sample_limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def compute_surprise(
    conn: sqlite3.Connection,
    content: str,
    memory_type: str | None,
    sample_limit: int = SAMPLE_LIMIT,
) -> dict:
    """计算单条内容的惊讶度。

    返回 {"surprise": float, "confidence": "low"|"normal", "compared": int}：
    - surprise = 1 - 与比对池的最大 jaccard（越不相似越惊讶）
    - compared < CONFIDENCE_MIN_SAMPLES → confidence="low"（冷启动样本不足）
    - 比对池为空（全新库）→ surprise=1.0, confidence="low", compared=0
    """
    sample = _fetch_sample(conn, memory_type, sample_limit)
    tokens = tokenize(content)
    max_sim = 0.0
    for row in sample:
        sim = jaccard(tokens, tokenize(row["content"]))
        if sim > max_sim:
            max_sim = sim
    surprise = round(1.0 - max_sim, 6)
    confidence = "normal" if len(sample) >= CONFIDENCE_MIN_SAMPLES else "low"
    return {
        "surprise": surprise,
        "confidence": confidence,
        "compared": len(sample),
    }


def batch_backfill(conn: sqlite3.Connection, limit: int = 1000) -> dict:
    """对缺少惊讶度分数的记忆批量计算（不写库，只返回统计）。

    统计口径：
    - computed：本轮计算的记忆条数（取最近 limit 条 active 记忆）
    - low_confidence：其中 confidence="low" 的条数（其所属类型池样本 < 30）
    - 每条记忆与其**同类型**池比对，且**排除自身**（自身相似度恒为 1.0，
      会把所有 backfill 分数压成 0，见测试 test_backfill_matches_compute_surprise）
    """
    rows = conn.execute(
        "SELECT memory_id, content, memory_type FROM memories "
        "WHERE status='active' ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()

    # 比对池按 memory_type 分组预载（active 全量），避免逐条回查
    pools: dict[str | None, list[dict]] = {}
    pool_rows = conn.execute(
        "SELECT memory_id, content, memory_type FROM memories WHERE status='active'"
    ).fetchall()
    for r in pool_rows:
        pools.setdefault(r["memory_type"], []).append(dict(r))

    computed = 0
    low_confidence = 0
    for row in rows:
        pool = pools.get(row["memory_type"], [])
        candidates = [r for r in pool if r["memory_id"] != row["memory_id"]]
        tokens = tokenize(row["content"])
        max_sim = 0.0
        for cand in candidates:
            sim = jaccard(tokens, tokenize(cand["content"]))
            if sim > max_sim:
                max_sim = sim
        computed += 1
        if len(candidates) < CONFIDENCE_MIN_SAMPLES:
            low_confidence += 1

    return {"computed": computed, "low_confidence": low_confidence}


# 模块自检：python -m sgme.engine.surprise 直接看分词/相似度行为
if __name__ == "__main__":
    demo = "今天下午三点开会讨论项目进度安排"
    print("tokenize:", sorted(tokenize(demo)))
    print("jaccard:", jaccard(tokenize(demo), tokenize(demo)))
