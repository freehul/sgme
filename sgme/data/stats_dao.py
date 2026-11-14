"""storage/stats_dao.py：统计查询——唯一统计 SQL 出口。

背景（2026-08-07 模块化重构 B30）：统计 SQL 曾散落 server 路由层
（routes_admin 7 处），违反「数据的 CRUD 只有 data 模块能实现」。
本模块收编全部统计查询；路由层只做鉴权、参数解析与响应组装。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("sgme.data.stats_dao")


def memory_summary(conn: sqlite3.Connection) -> dict:
    """记忆池计数：现行 + 归档。"""
    total = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    archived = conn.execute("SELECT COUNT(*) AS c FROM memory_archive").fetchone()["c"]
    return {"total": total, "archived": archived}


def dimension_distribution(conn: sqlite3.Connection) -> list[dict]:
    """维度分布：注册表维度 → 记忆标签计数（LEFT JOIN 保零）。"""
    rows = conn.execute(
        """
        SELECT d.id, d.display_name, COUNT(t.memory_id) AS cnt
        FROM dimension_registry d
        LEFT JOIN memory_tags t ON t.dimension_id = d.id
        GROUP BY d.id
        ORDER BY cnt DESC
        """
    ).fetchall()
    return [{"id": r["id"], "display_name": r["display_name"], "count": r["cnt"]} for r in rows]


def raw_files_summary(conn: sqlite3.Connection) -> dict:
    """原始层计数：总数 + 各状态 + 最后提炼水位。

    状态枚举（与 session_dao SESSION_STATUS_VALUES 一致）：
    new / refined / error / archived（dream 冷归档产生，2026-08-14 补齐统计，
    此前 archived 漏计导致 total ≠ new+refined+error 不自洽）。
    """
    total = conn.execute("SELECT COUNT(*) AS c FROM raw_files").fetchone()["c"]
    new = conn.execute("SELECT COUNT(*) AS c FROM raw_files WHERE status='new'").fetchone()["c"]
    refined = conn.execute("SELECT COUNT(*) AS c FROM raw_files WHERE status='refined'").fetchone()["c"]
    error = conn.execute("SELECT COUNT(*) AS c FROM raw_files WHERE status='error'").fetchone()["c"]
    archived = conn.execute("SELECT COUNT(*) AS c FROM raw_files WHERE status='archived'").fetchone()["c"]
    row = conn.execute(
        "SELECT MAX(refined_at) AS last FROM raw_files WHERE refined_at IS NOT NULL"
    ).fetchone()
    return {
        "total": total, "new": new, "refined": refined, "error": error,
        "archived": archived,
        "last_refined_at": row["last"] if row else None,
    }


def agent_last_seen(conn: sqlite3.Connection) -> dict[str, str]:
    """各 Agent 最后一次 append 会话的时间（来自 raw_files 聚合）。

    ⚠️ 语义硬约定：这是「该 Agent 最后一次 append 会话的时间」，
    **不是心跳**。禁止把它文档化或改名为 heartbeat。

    读库异常时返回空 dict（调用方降级为全部 last_seen_at=null）并记 WARN，
    绝不抛 —— 身份列表是主功能，活跃度只是增强信息，
    不得因活跃度查询失败拖垮主功能。
    """
    try:
        rows = conn.execute(
            """
            SELECT agent_id, MAX(COALESCE(ended_at, started_at)) AS last_seen
            FROM raw_files
            WHERE agent_id IS NOT NULL AND agent_id != ''
            GROUP BY agent_id
            """
        ).fetchall()
    except Exception as e:  # noqa: BLE001 —— 任何读库异常都必须降级而非 500
        logger.warning("聚合 last_seen_at 失败，降级为全部 null: %s", e)
        return {}
    out: dict[str, str] = {}
    for r in rows:
        if r["last_seen"]:
            out[r["agent_id"]] = r["last_seen"]
    return out

# ---------- token 成本/质量明细（0.8 T-15 / 契约 §5.7） ----------

#: 聚合粒度 → period_key 表达式。
#: weekly 用 ``date(x,'weekday 0','-6 days')``：'weekday 0' 前进到本周日
#: （已是周日则不动），再减 6 天得**周一**，即 ISO 周起（周一~周日）。
#: 三种粒度都返回 ``YYYY-MM-DD`` 形态，UI 侧无需分支处理。
_PERIOD_EXPRESSIONS: dict[str, str] = {
    "daily": "strftime('%Y-%m-%d', started_at)",
    "weekly": "date(started_at, 'weekday 0', '-6 days')",
    "monthly": "strftime('%Y-%m-01', started_at)",
}

#: 参与求和的计数列（items 与 totals 共用，避免两处漏改）。
_DETAIL_SUM_FIELDS: tuple[str, ...] = (
    "runs", "ok", "error",
    "prompt_tokens", "completion_tokens", "total_tokens", "memories_count",
    # ST-58/T-296（D1）：耗时列随 A2（T-289）落地后纳入聚合——效率由此可观测（G4）。
    "elapsed_ms", "elapsed_ms_runs",
)

#: 派生字段（**不对 totals 求和**——比值类字段相加无意义）。
_DETAIL_DERIVED_FIELDS: tuple[str, ...] = (
    "tokens_per_memory",   # total_tokens / memories_count：单条记忆的提炼成本
    "avg_prompt_tokens",   # prompt_tokens / runs：单批平均 prompt 体积
    "elapsed_ms_avg",      # elapsed_ms / elapsed_ms_runs：单批真实平均耗时
    "batch_utilization",   # avg_prompt_tokens / batch_budget：批预算利用率（需外部传入预算）
)


def refine_detail(
    conn: sqlite3.Connection,
    *,
    period: str = "weekly",
    stage: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    batch_budget: int | None = None,
) -> tuple[list[dict], dict]:
    """提炼 token 成本 / 质量明细聚合（契约 §5.7）。

    B30 铁律落点：本函数是 stats/detail 端点的**唯一聚合 SQL 出口**，
    路由与 operations 层一行 SQL 都不写。

    分组恒为 ``(period_key, stage)``——契约「无 stage 参数时按 stage 分行」；
    传了 stage 则结果自然只剩该 stage 的行，无需分支。

    ``totals`` 由 items 在 Python 侧求和而非再跑一次 SQL：两次查询之间若有并发
    写入会导致 totals 与 items 对不上，用户看到「分项加起来 ≠ 合计」。
    **派生字段不进 totals**（比值相加无意义，见 `_DETAIL_DERIVED_FIELDS`）。

    Args:
        conn: memory.db 连接（refine_runs 在 memory.db）。
        period: ``daily`` / ``weekly`` / ``monthly``。
        stage: 可选 stage 过滤。
        from_ts / to_ts: 作用于 ``started_at`` 的闭区间边界。
        batch_budget: 上下文批预算（tokens），用于算派生字段
            ``batch_utilization = avg_prompt_tokens / batch_budget``。
            由 operations 层从 cfg 解析后传入（本层不读配置，保持 data 层纯粹）；
            不传则该字段为 ``None``（其余派生字段照常给出）。

    Returns:
        ``(items, totals)``——items 按 ``period_key ASC, stage ASC`` 排序
        （时间升序便于直接喂图表）；totals 为各计数列的合计。

    Raises:
        ValueError: ``period`` 不在白名单内（正常路径下 operations 层已拦截）。
    """
    period_expr = _PERIOD_EXPRESSIONS.get(period)
    if period_expr is None:
        raise ValueError(f"不支持的聚合粒度: {period}")

    where: list[str] = []
    params: list = []
    if stage:
        where.append("stage = ?")
        params.append(stage)
    if from_ts:
        where.append("started_at >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("started_at <= ?")
        params.append(to_ts)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    rows = conn.execute(
        f"""
        SELECT {period_expr} AS period_key,
               stage AS stage,
               COUNT(*) AS runs,
               SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error,
               SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
               SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
               SUM(COALESCE(total_tokens, 0)) AS total_tokens,
               SUM(COALESCE(memories_count, 0)) AS memories_count,
               SUM(COALESCE(elapsed_ms, 0)) AS elapsed_ms,
               SUM(CASE WHEN elapsed_ms IS NOT NULL THEN 1 ELSE 0 END) AS elapsed_ms_runs
        FROM refine_runs{where_sql}
        GROUP BY period_key, stage
        ORDER BY period_key ASC, stage ASC
        """,
        params,
    ).fetchall()

    items: list[dict] = []
    totals: dict[str, int] = {f: 0 for f in _DETAIL_SUM_FIELDS}
    for r in rows:
        item: dict = {"period_key": r["period_key"], "stage": r["stage"]}
        for f in _DETAIL_SUM_FIELDS:
            value = int(r[f] or 0)
            item[f] = value
            totals[f] += value
        item.update(_derive_detail_fields(item, batch_budget=batch_budget))
        items.append(item)
    return items, totals


def _derive_detail_fields(item: dict, *, batch_budget: int | None) -> dict:
    """由聚合计数派生比值字段（**不参与 totals 求和**）。

    分母为 0 时统一返回 ``None``（而不是 0）——「没有数据」与「比值是 0」
    语义不同，前端据此区分「无样本」与「极低成本」。

    - ``tokens_per_memory``：单条记忆的提炼成本（成本集中度的核心观测量）
    - ``avg_prompt_tokens``：单批平均 prompt 体积
    - ``elapsed_ms_avg``：单批真实平均耗时（只对**有耗时值**的行求均值，
      老行 ``elapsed_ms IS NULL`` 不计入分母，避免拉低均值）
    - ``batch_utilization``：批预算利用率 = avg_prompt_tokens / batch_budget
      （批预算由调用方传入；不传则 None）
    """
    runs = int(item.get("runs") or 0)
    memories = int(item.get("memories_count") or 0)
    total_tokens = int(item.get("total_tokens") or 0)
    prompt_tokens = int(item.get("prompt_tokens") or 0)
    elapsed_runs = int(item.get("elapsed_ms_runs") or 0)
    elapsed_ms = int(item.get("elapsed_ms") or 0)

    tokens_per_memory = round(total_tokens / memories, 2) if memories > 0 else None
    avg_prompt_tokens = round(prompt_tokens / runs, 1) if runs > 0 else None
    elapsed_ms_avg = round(elapsed_ms / elapsed_runs, 1) if elapsed_runs > 0 else None
    utilization = None
    if batch_budget and avg_prompt_tokens is not None:
        utilization = round(avg_prompt_tokens / batch_budget, 4)

    return {
        "tokens_per_memory": tokens_per_memory,
        "avg_prompt_tokens": avg_prompt_tokens,
        "elapsed_ms_avg": elapsed_ms_avg,
        "batch_utilization": utilization,
    }


def count_vector_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """向量表行数统计（ST-22② 健康检查向量可用性用，T-9 收口自 operations/health.py）。

    - 表缺失按 0 计（sqlite-vec 未挂载 / 旧库无向量表 → 不抛异常）；
    - 单表 COUNT 异常按 0 计——只读统计，**绝不向上抛**（健康检查必须健壮，
      任何探测失败只影响 reason 文案，不拖垮 /v1/health）。

    Args:
        conn: memory.db 连接（memory_vectors / scene_vectors 所在库）。

    Returns:
        dict：``{"memory_vectors": int, "scene_vectors": int}``。
    """
    counts = {"memory_vectors": 0, "scene_vectors": 0}
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    except Exception:
        return counts
    for table in counts:
        if table not in tables:
            continue
        try:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            counts[table] = row["c"] if row else 0
        except Exception:
            counts[table] = 0
    return counts


# ---------- 记忆组成指标（v0.3 S5，2026-11-14） ----------
#
# 背景：记忆池「太重」的量化口径——技术+环境占标签约 2/3，而「关于人」的部分被挤。
# 决策 D2（2026-11-14 用户拍板）：**不做硬门禁**，只做观测 + 告警。
# 理由：80 字短条目里 ratio 极易被操纵（把技术内容写成「用户偏好折腾 X」就达标），
# 拿它当闸会诱导模型造假；真正判好坏的是 200 条抽样人工评审。

#: 「关于人」的维度（严格的文档口径，v0.3 §7）
PERSON_DIMENSIONS = frozenset({
    "identity", "family", "social", "preferences",
    "habits", "status", "goals", "values",
})

#: 扩展口径：把「关注 / 能力 / 创意」也算进来（这些同样是人的属性，
#: 但更容易和技术内容混同，故与严格口径分开报，供对照判读）
PERSON_DIMENSIONS_EXTENDED = PERSON_DIMENSIONS | {"focus", "skills", "ideas"}

#: 技术类维度
TECH_DIMENSIONS = frozenset({"tech_stack", "environment"})


def _covered(conn: sqlite3.Connection, dims: frozenset[str]) -> int:
    """去重后的记忆覆盖数（一条记忆多维度只算一次，避免 ratio 超过 1）。"""
    if not dims:
        return 0
    ph = ",".join("?" * len(dims))
    row = conn.execute(
        f"SELECT COUNT(DISTINCT memory_id) AS c FROM memory_tags "
        f"WHERE dimension_id IN ({ph})",
        tuple(sorted(dims)),
    ).fetchone()
    return int(row["c"]) if row else 0


def memory_composition(conn: sqlite3.Connection) -> dict:
    """记忆组成快照：人/技术占比 + 路由分布 + 长度 + 待归类积压。

    口径说明（写死在这里，避免各调用方各算各的）：
    - total：memories 全量（含 needs_review 待审，它们也是记忆）；
    - person_ratio = 覆盖 person 维度的记忆数 / total（**不互斥**，一条记忆可同时算人又算技术）；
    - route_dist：按 memories.route 分组，NULL 归"(null)"；
    - inbox_pending：routing_inbox 待处理条数（分流兜底积压，恶化信号）。

    Returns:
        {total, person_covered, person_ratio, person_covered_extended,
         person_ratio_extended, tech_covered, tech_ratio, route_dist,
         avg_len, max_len, inbox_pending}
    """
    total = int(conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"] or 0)
    if total == 0:
        return {
            "total": 0, "person_covered": 0, "person_ratio": 0.0,
            "person_covered_extended": 0, "person_ratio_extended": 0.0,
            "tech_covered": 0, "tech_ratio": 0.0, "route_dist": {},
            "avg_len": 0.0, "max_len": 0, "inbox_pending": 0,
        }
    person_n = _covered(conn, PERSON_DIMENSIONS)
    person_ext_n = _covered(conn, PERSON_DIMENSIONS_EXTENDED)
    tech_n = _covered(conn, TECH_DIMENSIONS)
    route_dist = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT COALESCE(route, '(null)') AS k, COUNT(*) AS c FROM memories GROUP BY k"
        )
    }
    lens = conn.execute(
        "SELECT AVG(LENGTH(content)) AS a, MAX(LENGTH(content)) AS m FROM memories"
    ).fetchone()
    try:
        inbox_pending = int(conn.execute(
            "SELECT COUNT(*) AS c FROM routing_inbox WHERE status = 'pending'"
        ).fetchone()["c"] or 0)
    except sqlite3.Error:
        inbox_pending = 0  # 老库未建表时不报错
    return {
        "total": total,
        "person_covered": person_n,
        "person_ratio": round(person_n / total, 4),
        "person_covered_extended": person_ext_n,
        "person_ratio_extended": round(person_ext_n / total, 4),
        "tech_covered": tech_n,
        "tech_ratio": round(tech_n / total, 4),
        "route_dist": route_dist,
        "avg_len": round(float((lens["a"] if lens else 0) or 0), 1),
        "max_len": int((lens["m"] if lens else 0) or 0),
        "inbox_pending": inbox_pending,
    }


def occurred_reliability(conn: sqlite3.Connection, *, recent_days: int = 7) -> dict:
    """时间锚可信度：active 记忆里 occurred_at 与 created_at 完全相等的占比。

    口径（D-26 / B323，2026-11-13）：落库兜底 o_at = occurred_at or created_at 会让
    映射失败的新记忆看起来「有值但等于入库时间」。本指标用于发现「时间轴退化为提炼
    时间」这类静默语义失真——正常应远低于 1.0；接近 1.0 说明提示词/映射链断了。

    T4（三缺口补齐 2026-11-14）增量/存量分离：
    - **存量口径**（全量 active）：``equal_ratio``——health 展示用；42k 存量超标是
      B323 修复前的历史问题，只展示不告警；
    - **增量口径**：最近 ``recent_days`` 天 ``created_at`` 的记忆单独算
      ``recent_ratio``——只有它超标才发 anomaly_warn（新提炼链断了必须立刻可见）。
      cutoff 由 Python 侧按同格式 ISO 计算后传参比较（created_at 为
      ``YYYY-MM-DDTHH:MM:SSZ``，同格式字典序可比，避开 SQLite datetime() 的
      空格格式混排）。

    Returns:
        {"active_total", "comparable", "equal_to_created", "equal_ratio",
        "recent_days", "recent_total", "recent_comparable",
        "recent_equal_to_created", "recent_ratio"}；无样本时 *ratio 为 None。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN occurred_at = created_at THEN 1 ELSE 0 END) AS equal_cnt, "
        "       SUM(CASE WHEN occurred_at IS NOT NULL AND created_at IS NOT NULL "
        "                THEN 1 ELSE 0 END) AS comparable "
        "FROM memories WHERE status = ?",
        ("active",),
    ).fetchone()
    total = int(row["total"] or 0)
    equal = int(row["equal_cnt"] or 0)
    comparable = int(row["comparable"] or 0)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=recent_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN occurred_at = created_at THEN 1 ELSE 0 END) AS equal_cnt, "
        "       SUM(CASE WHEN occurred_at IS NOT NULL AND created_at IS NOT NULL "
        "                THEN 1 ELSE 0 END) AS comparable "
        "FROM memories WHERE status = ? AND created_at >= ?",
        ("active", cutoff),
    ).fetchone()
    r_total = int(recent["total"] or 0)
    r_equal = int(recent["equal_cnt"] or 0)
    r_comparable = int(recent["comparable"] or 0)
    return {
        "active_total": total,
        "comparable": comparable,
        "equal_to_created": equal,
        "equal_ratio": round(equal / comparable, 4) if comparable else None,
        "recent_days": recent_days,
        "recent_total": r_total,
        "recent_comparable": r_comparable,
        "recent_equal_to_created": r_equal,
        "recent_ratio": round(r_equal / r_comparable, 4) if r_comparable else None,
    }
