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

import logging
import re
import sqlite3

logger = logging.getLogger("sgme.engine.surprise")

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


def _percentile_of(
    conn: sqlite3.Connection,
    memory_type: str | None,
    value: float,
) -> tuple[float | None, int]:
    """把绝对惊讶度映射成「同类型内分位」（D-78 要求）。

    返回值：(分位 或 None, 分布样本数)。样本数 < ``CONFIDENCE_MIN_SAMPLES`` 时返回
    None——冷启动期不能拿十几条样本算分位，否则前几条记忆的分位永远 0/0.5/1。
    """
    sql = "SELECT surprise FROM memories WHERE status='active' AND surprise IS NOT NULL"
    params: list = []
    if memory_type is not None:
        sql += " AND memory_type=?"
        params.append(memory_type)
    rows = conn.execute(sql, params).fetchall()
    vals = [float(r[0]) for r in rows if r[0] is not None]
    n = len(vals)
    if n < CONFIDENCE_MIN_SAMPLES:
        return None, n
    below = sum(1 for v in vals if v < value)
    return round(below / n, 6), n


def compute_for_store(
    conn,
    content: str | None,
    memory_type: str | None,
    sample_limit: int = 150,
) -> float | None:
    """落库钩子：算一条新记忆的惊讶度分数（D-78 接线点）。

    **D-78 口径**：库里存的是**同类型内分位**（0–1），不是绝对相似度——
    绝对值的含义随类型差异漂移（同一句话在「事实」池里不意外，在「经验」池里很意外），
    跨类型不可比，没法给 D-50 当先验、也没法给 D-29 合并闸当判据。

    实现分两步：先用绝对 ``1 − max_jaccard`` 定位，再映射到同类型已存分位的分布。
    同类型样本 < ``CONFIDENCE_MIN_SAMPLES``（冷启动）时**退回绝对值**——此时算分位
    没有统计意义；混合期（绝对值 + 分位）里绝对值占比会随样本增长自然稀释。

    **旁路语义**：任何异常都返回 None，绝不阻塞落库——惊讶度是排序先验，
    没有它记忆照样该存。``sample_limit`` 取 150 而非默认 300：落库是热路径，
    每条记忆都要扫同类样本，控制在几十毫秒内。
    """
    try:
        text = (content or "").strip()
        if not text:
            # 空内容没有"惊讶"可言：返回 None（= 未计算），而不是 1.0 冒充最高惊讶
            return None
        res = compute_surprise(conn, text, memory_type, sample_limit=sample_limit)
        absolute = float(res.get("surprise") or 0.0)
        pct, n = _percentile_of(conn, memory_type, absolute)
        if pct is None:
            logger.debug(
                "惊讶度分位样本不足（同类型 %d 条 < %d），存绝对值 %.4f",
                n, CONFIDENCE_MIN_SAMPLES, absolute,
            )
            return absolute
        return pct
    except Exception as exc:  # noqa: BLE001——先验计算失败不得影响存储
        logger.warning("惊讶度计算失败（不阻塞落库）: %s", exc)
        return None


# 模块自检：python -m sgme.engine.surprise 直接看分词/相似度行为
if __name__ == "__main__":
    demo = "今天下午三点开会讨论项目进度安排"
    print("tokenize:", sorted(tokenize(demo)))
    print("jaccard:", jaccard(tokenize(demo), tokenize(demo)))
