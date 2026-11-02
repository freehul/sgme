"""llm_usage_dao.py：LLM 用量按天账本 DAO（设计第12项 / D-63）。

数据流：``engine/cost_meter.py``（extract/backfill）→ ``record_usage()``
（按天×供应商×模型×阶段聚合 upsert 累加）→ ``daily_summary()`` /
``total_by_model()`` 供成本告警与观测查询。

设计要点：
- 表 ``llm_usage_daily`` 由 ``ensure_table()`` 自建（CREATE TABLE IF NOT EXISTS
  幂等），**不改 db.py 既有表定义**——账本属计量旁路，不随 schema 版本演进
- 行粒度 = (day, provider, model, stage)：一天一个供应商一个模型一个阶段一行；
  空间 O(天 × 供应商 × 模型 × 阶段)
- day 取记录时刻（finished_at）的 UTC 日期前 10 位（"YYYY-MM-DD"）
- 与 usage_dao.py（T-163 接口调用统计，表 api_usage_daily）分工：那个记
  「谁调了多少次 API」，本模块记「LLM 花了多少 token」——两者互不替代
- 写入方全静默约定同 usage_dao：统计是旁路，调用点（cost_meter）自行 try/except

分层：本模块只做 SQL，不做业务判断（记账元组抽取在 engine/cost_meter 完成）。
"""

from __future__ import annotations

import sqlite3


def ensure_table(conn: sqlite3.Connection) -> None:
    """建 llm_usage_daily 账本表（CREATE TABLE IF NOT EXISTS，幂等）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_usage_daily (
          day TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          stage TEXT NOT NULL,
          prompt_tokens INTEGER DEFAULT 0,
          completion_tokens INTEGER DEFAULT 0,
          calls INTEGER DEFAULT 0,
          PRIMARY KEY (day, provider, model, stage)
        )
        """
    )
    conn.commit()


def record_usage(
    conn: sqlite3.Connection,
    day: str,
    provider: str,
    model: str,
    stage: str,
    prompt_tokens: int,
    completion_tokens: int,
    calls: int = 1,
) -> None:
    """记一笔用量到账本（按 (day, provider, model, stage) 聚合累加）。

    UPSERT 累加：同桶已有行则 tokens 与 calls 叠加（ON CONFLICT DO UPDATE），
    天然幂等于「同桶并发写」——但**不幂等于「同一批历史重放」**（重复回填会
    双倍累加，防重放是 cost_meter.backfill 的职责，见其游标设计）。
    """
    conn.execute(
        """
        INSERT INTO llm_usage_daily
          (day, provider, model, stage, prompt_tokens, completion_tokens, calls)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(day, provider, model, stage) DO UPDATE SET
          prompt_tokens = prompt_tokens + excluded.prompt_tokens,
          completion_tokens = completion_tokens + excluded.completion_tokens,
          calls = calls + excluded.calls
        """,
        (day, provider, model, stage, int(prompt_tokens), int(completion_tokens), int(calls)),
    )
    conn.commit()


def daily_summary(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """最近 N 天的账本行（day DESC；每行一个 (day, provider, model, stage) 桶）。

    - 窗口锚定「今天」：day >= today - (days-1)，与 usage_dao.query_usage 同口径
    - days < 1 视为 1（至少今天）
    """
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC) - timedelta(days=max(1, days) - 1)).date().isoformat()
    rows = conn.execute(
        """
        SELECT day, provider, model, stage, prompt_tokens, completion_tokens, calls
        FROM llm_usage_daily
        WHERE day >= ?
        ORDER BY day DESC, provider, model, stage
        """,
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def total_by_model(conn: sqlite3.Connection) -> list[dict]:
    """全期按 model 汇总（tokens 总量 + 调用次数；calls DESC 排序）。"""
    rows = conn.execute(
        """
        SELECT model,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens,
               SUM(calls) AS calls
        FROM llm_usage_daily
        GROUP BY model
        ORDER BY calls DESC, model
        """
    ).fetchall()
    return [dict(r) for r in rows]
