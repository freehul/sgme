"""engine/meaning_anchor.py：D-50 意义锚 M（2.0 · 方向修正版 v2，纯 SQL/零 LLM）。

## 为什么不是文档原版的「四主料等权」

9.11.4 / 9.11.5 两轮实测（2026-10-30，生产库只读）：
- 等权版 M 的 p10–max 只有 **0.268–0.865**，按旧绝对阈值 0.80/0.50 分档 → 高 0.1% / 中 13.9% / 低 86.0%（塌缩）；
- 人工抽检 40 条（高/低档各 20）判「一年后是否仍有用」：档命中率 **35% vs 30%** → **区分力不成立**。

失效原因是**方向性错误**，不是权重没调好：
1. 四主料全是结构信号，没有一个读内容；
2. tech_stack 占 48.9% 却给次高分 —— 技术细节恰恰最该衰减；
3. 时间稳定性奖励「老」——老的技术细节同样受益；
4. 结构入度偏向互引密集的技术记忆。

## 本实现（设计 9.11.5「方案 B」）

主料（合计 0.75）：
- 维度先验 0.30 —— v2 档位按「该不该长期保留」重排（family/values/identity 最高，
  **tech_stack 降到 0.35**，environment/preferences 提档：长期工作偏好事实上最耐放）
- 时间稳定性 0.20 —— ln(1+occurred_at 距今天数)/ln(731)，2 年封顶
- 结构入度 0.15 —— ln(1+被引用数)/ln(1+表内最大入度)；缺边按 0（不拉低过多，见 9.11.3 覆盖 67%）
- 细节充分度 0.10 —— min(1, 正文字数/200)（9.11.3：mean 76 字，p90 137）

内容信号（0.25，零成本关键词，不读语义、不调 LLM）：
- 长期/规则性词（一直/总是/固定/原则/底线/必须/禁止/每次/长期…）加分
- 一次性任务词（这次/临时/今天/试试/帮我/改一下…）减分
- 基线 0.5：无信号时中性，避免「没关键词=低意义」

加分项（各 0.05，封顶 0.10）：
- feeling 非空（情绪先验弱信号，D-51）
- recall_count > 0（真实使用证据，D-73 同源）
- ⬜ 人工标注 my_notes 尚未落到 memories 列（D-31 记忆详情页未做），落地后接这里。

## 阈值

**不写死绝对分**：9.11.4 已证绝对阈值不适用；D-52 的分档按**分位**（p67 / p33，见
config ttl.disposition.tier_high_ratio / tier_low_ratio），由 tier_thresholds() 现算。
"""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import UTC, datetime

logger = logging.getLogger("sgme.engine.meaning_anchor")

# v2 维度先验：数值 = 「一年后仍值得保留」的先验强度。
# 与 D-50 原档位的差异见模块头注释（tech_stack 降档是核心修正）。
DIMENSION_PRIOR: dict[str, float] = {
    "identity": 1.00,
    "values": 1.00,
    "family": 0.95,
    "preferences": 0.90,
    "environment": 0.88,
    "habits": 0.85,
    "social": 0.85,
    "skills": 0.70,
    "goals": 0.60,
    "focus": 0.50,
    "ideas": 0.45,
    "tech_stack": 0.35,
    "status": 0.25,
}
DEFAULT_PRIOR = 0.55  # 未知/多标签全部缺失时的中性值

# 内容信号词表（零成本；命中即计数，不做语义理解）
LONG_TERM_KEYWORDS: tuple[str, ...] = (
    "一直", "总是", "从来", "从不", "每次", "通常", "固定", "习惯", "原则", "底线",
    "必须", "禁止", "不要", "一律", "约定", "长期", "永久", "以后都", "今后",
    "从来不用", "只吃", "只喝", "不喝", "不吃", "过敏", "忌讳",
    "never", "always", "usually", "prefer",
)
ONE_OFF_KEYWORDS: tuple[str, ...] = (
    "这次", "临时", "今天", "刚才", "暂时", "试试", "先这样", "帮我", "改一下",
    "看一下", "跑一下", "修一下", "debug", "临时改", "这一次",
)

_TIME_SPAN_DAYS = 731.0  # 2 年（含闰年）作为「稳定」封顶


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def content_signal(content: str) -> float:
    """内容侧零成本信号（0–1）：长期/规则词加分，一次性任务词减分。

    基线 0.5 = 无信号中性；每命中 1 个长期词 +0.15（最多 3 个），
    每命中 1 个一次性词 -0.10（最多 3 个）。
    """
    text = content or ""
    long_hits = sum(1 for kw in LONG_TERM_KEYWORDS if kw in text)
    oneoff_hits = sum(1 for kw in ONE_OFF_KEYWORDS if kw in text)
    score = 0.5 + 0.15 * min(long_hits, 3) - 0.10 * min(oneoff_hits, 3)
    return max(0.0, min(1.0, score))


def compute_m(
    *,
    content: str,
    dimension_prior: float,
    occurred_at: str | None,
    updated_at: str | None,
    in_degree: int,
    max_in_degree: int,
    has_feeling: bool = False,
    recall_count: int = 0,
    now: datetime | None = None,
) -> float:
    """单条记忆的意义锚 M（0–1）。纯函数，便于单测与离线回放。"""
    now = now or _now()
    # ① 维度先验
    prior = max(0.0, min(1.0, dimension_prior))

    # ② 时间稳定性：occurred_at 距今越久 = 越稳定（有 occurred_at 用它，否则回落 updated_at）
    ref = _parse_iso(occurred_at) or _parse_iso(updated_at)
    if ref is None:
        stability = 0.0
    else:
        days = max(0.0, (now - ref).total_seconds() / 86400.0)
        stability = min(1.0, math.log(1.0 + days) / math.log(1.0 + _TIME_SPAN_DAYS))

    # ③ 结构入度：被引用越多 = 越有人真的在用
    if max_in_degree <= 0 or in_degree <= 0:
        indeg = 0.0
    else:
        indeg = min(1.0, math.log(1.0 + in_degree) / math.log(1.0 + max_in_degree))

    # ④ 细节充分度
    detail = min(1.0, len((content or "").strip()) / 200.0)

    # ⑤ 内容信号
    csig = content_signal(content)

    m = (
        0.30 * prior
        + 0.20 * stability
        + 0.15 * indeg
        + 0.10 * detail
        + 0.25 * csig
    )
    # 加分项（封顶 0.10）：情绪先验 + 使用证据
    bonus = 0.0
    if has_feeling:
        bonus += 0.05
    if recall_count and recall_count > 0:
        bonus += 0.05
    return max(0.0, min(1.0, m + min(0.10, bonus)))


def _prior_sql() -> str:
    """维度先验 SQL 片段：MIN(CASE ...)（多标签取最高意义的一维）。"""
    cases = " ".join(f"WHEN '{dim}' THEN {score}" for dim, score in DIMENSION_PRIOR.items())
    return f"MIN(CASE t.dimension_id {cases} ELSE {DEFAULT_PRIOR} END)"


def _in_degree_map(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "memory_edges"):
        return {}
    rows = conn.execute(
        "SELECT to_id, COUNT(*) FROM memory_edges GROUP BY to_id"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows if r[0]}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def recompute(
    conn: sqlite3.Connection,
    *,
    only_null: bool = False,
    now: datetime | None = None,
) -> dict:
    """重算并落库 meaning_anchor，返回统计（count / distribution / thresholds）。

    Args:
        conn: memory.db 连接。
        only_null: 只补 NULL 行（梦境每日任务用；全量回填走脚本 --apply）。
        now: 注入时钟（测试用）。
    """
    now = now or _now()
    deg = _in_degree_map(conn)
    max_deg = max(deg.values()) if deg else 0

    where = "m.status='active'"
    if only_null:
        where += " AND m.meaning_anchor IS NULL"
    sql = f"""
        SELECT m.memory_id, m.content, m.occurred_at, m.updated_at, m.feeling,
               COALESCE(s.recall_count, 0) AS recall_count,
               (SELECT {_prior_sql()} FROM memory_tags t WHERE t.memory_id = m.memory_id) AS prior
        FROM memories m
        LEFT JOIN memory_stats s ON s.memory_id = m.memory_id
        WHERE {where}
    """
    rows = conn.execute(sql).fetchall()
    updates: list[tuple[float, str]] = []
    values: list[float] = []
    for r in rows:
        prior = r["prior"] if r["prior"] is not None else DEFAULT_PRIOR
        m_value = compute_m(
            content=r["content"] or "",
            dimension_prior=float(prior),
            occurred_at=r["occurred_at"],
            updated_at=r["updated_at"],
            in_degree=deg.get(str(r["memory_id"]), 0),
            max_in_degree=max_deg,
            has_feeling=bool(r["feeling"]),
            recall_count=int(r["recall_count"] or 0),
            now=now,
        )
        updates.append((m_value, str(r["memory_id"])))
        values.append(m_value)

    if updates:
        conn.executemany(
            "UPDATE memories SET meaning_anchor=? WHERE memory_id=?", updates
        )
        conn.commit()

    return {
        "count": len(updates),
        "distribution": _percentiles(values) if values else {},
        "max_in_degree": max_deg,
        "only_null": only_null,
    }


def _percentiles(values: list[float]) -> dict:
    """分位统计（p10/p33/p50/p67/p90/max/mean，线性插值）。"""
    if not values:
        return {}
    ordered = sorted(values)

    def q(ratio: float) -> float:
        if len(ordered) == 1:
            return round(ordered[0], 4)
        pos = ratio * (len(ordered) - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return round(ordered[lo], 4)
        w = pos - lo
        return round(ordered[lo] * (1 - w) + ordered[hi] * w, 4)

    return {
        "n": len(ordered),
        "p10": q(0.10),
        "p33": q(0.33),
        "p50": q(0.50),
        "p67": q(0.67),
        "p90": q(0.90),
        "max": round(ordered[-1], 4),
        "min": round(ordered[0], 4),
        "mean": round(sum(ordered) / len(ordered), 4),
    }


def distribution(conn: sqlite3.Connection) -> dict:
    """当前 meaning_anchor 分布（只读，供运维页与脚本）。"""
    rows = conn.execute(
        "SELECT meaning_anchor FROM memories WHERE status='active' "
        "AND meaning_anchor IS NOT NULL"
    ).fetchall()
    return _percentiles([float(r[0]) for r in rows])


def tier_thresholds(
    conn: sqlite3.Connection, high_ratio: float = 0.67, low_ratio: float = 0.33
) -> tuple[float | None, float | None]:
    """按分位算 D-52 两档阈值（high = p{high_ratio}，low = p{low_ratio}）。"""
    rows = conn.execute(
        "SELECT meaning_anchor FROM memories WHERE status='active' "
        "AND meaning_anchor IS NOT NULL ORDER BY meaning_anchor"
    ).fetchall()
    values = [float(r[0]) for r in rows]
    if len(values) < 3:
        return None, None
    import math as _math

    def q(ratio: float) -> float:
        pos = ratio * (len(values) - 1)
        lo = int(_math.floor(pos))
        hi = int(_math.ceil(pos))
        if lo == hi:
            return values[lo]
        w = pos - lo
        return values[lo] * (1 - w) + values[hi] * w

    return q(high_ratio), q(low_ratio)
