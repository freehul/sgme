"""storage/stats_dao.py：统计查询——唯一统计 SQL 出口。

背景（2026-08-07 模块化重构 B30）：统计 SQL 曾散落 server 路由层
（routes_admin 7 处），违反「数据的 CRUD 只有 data 模块能实现」。
本模块收编全部统计查询；路由层只做鉴权、参数解析与响应组装。
"""
from __future__ import annotations

import logging
import sqlite3

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
)


def refine_detail(
    conn: sqlite3.Connection,
    *,
    period: str = "weekly",
    stage: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> tuple[list[dict], dict]:
    """提炼 token 成本 / 质量明细聚合（契约 §5.7）。

    B30 铁律落点：本函数是 stats/detail 端点的**唯一聚合 SQL 出口**，
    路由与 operations 层一行 SQL 都不写。

    分组恒为 ``(period_key, stage)``——契约「无 stage 参数时按 stage 分行」；
    传了 stage 则结果自然只剩该 stage 的行，无需分支。

    ``totals`` 由 items 在 Python 侧求和而非再跑一次 SQL：两次查询之间若有并发
    写入会导致 totals 与 items 对不上，用户看到「分项加起来 ≠ 合计」。

    Args:
        conn: memory.db 连接（refine_runs 在 memory.db）。
        period: ``daily`` / ``weekly`` / ``monthly``。
        stage: 可选 stage 过滤。
        from_ts / to_ts: 作用于 ``started_at`` 的闭区间边界。

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
               SUM(COALESCE(memories_count, 0)) AS memories_count
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
        items.append(item)
    return items, totals
