"""cost_meter.py：LLM 用量计量引擎（设计第12项 / D-63）。

职责：把 refine_runs 批次行翻译为记账元组，写入 ``llm_usage_daily`` 按天账本
（DAO 见 ``sgme/data/llm_usage_dao.py``），供成本告警按天取数。

两条路径：
- ``extract_from_refine_run``：单行抽取（实时记账调用点用——提炼批次 finish 后
  取该行抽元组 → ``llm_usage_dao.record_usage``）
- ``backfill_from_refine_runs``：历史回填（只读 refine_runs，写账本 + 游标表）

设计要点：
- refine_runs 现无 model 列（v3 schema）：model 取行内 ``model`` 字段，缺列回退
  ``provider``——按供应商分桶足以支撑成本告警；将来加列零改动兼容
- ``finished_at IS NULL`` = 批次未正常收尾（running/被收敛的悬挂行）：跳过不记，
  day 无法确定且 tokens 不可信
- 回填幂等靠游标表 ``llm_usage_backfill``（run_id 主键，标记已回填批次）：
  record_usage 是纯累加，重放同一批会双倍计费，必须靠「每批只记一次」挡住；
  先例同 session.db 的 refine_cursor 水位表。refine_runs 保持只读（只 SELECT）
- 记账是旁路：调用点（refine finish 后）应 try/except 包裹，失败不影响提炼主链路
"""

from __future__ import annotations

import logging
import sqlite3

from sgme.data import llm_usage_dao

logger = logging.getLogger("sgme.engine.cost_meter")


def extract_from_refine_run(run_row: dict) -> dict | None:
    """从 refine_runs 一行抽取记账元组；不可记账返回 None。

    - finished_at 为 None（未收尾批次）→ None
    - provider / stage 缺失（无法分桶）→ None
    - model 缺列（refine_runs 无该列）→ 回退 provider
    - tokens 为 NULL → 归 0（调用次数仍要记，防调用数漏计）
    - day = finished_at 前 10 位（"YYYY-MM-DD"）
    """
    finished_at = run_row.get("finished_at")
    if not finished_at:
        return None
    provider = run_row.get("provider")
    stage = run_row.get("stage")
    if not provider or not stage:
        return None
    return {
        "day": str(finished_at)[:10],
        "provider": provider,
        "model": run_row.get("model") or provider,
        "stage": stage,
        "prompt_tokens": int(run_row.get("prompt_tokens") or 0),
        "completion_tokens": int(run_row.get("completion_tokens") or 0),
    }


def backfill_from_refine_runs(conn: sqlite3.Connection, limit: int = 5000) -> dict:
    """从 refine_runs 回填历史用量到 llm_usage_daily（幂等，可重复执行）。

    - 只读 refine_runs（SELECT finished_at 非空的行），写账本 + 游标标记
    - 游标表 ``llm_usage_backfill``（run_id 主键）：已回填批次不再重放——
      record_usage 是纯累加，无标记则跑两次 calls 翻倍
    - limit：本次最多回填多少批（按 finished_at ASC 顺序吃，剩余留给下次）
    - 返回 {"backfilled_days": 本次写入涉及的去重天数, "rows": 本次回填批次数}
    """
    llm_usage_dao.ensure_table(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_usage_backfill (
          run_id TEXT PRIMARY KEY,
          marked_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    rows = conn.execute(
        """
        SELECT r.run_id, r.stage, r.provider, r.prompt_tokens,
               r.completion_tokens, r.finished_at
        FROM refine_runs r
        WHERE r.finished_at IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM llm_usage_backfill b WHERE b.run_id = r.run_id)
        ORDER BY r.finished_at ASC, r.run_id ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()

    marked_at_rows: list[tuple[str, str]] = []
    days: set[str] = set()
    for row in rows:
        run = dict(row)
        usage = extract_from_refine_run(run)
        if usage is None:  # 理论不可达（finished_at/provider/stage 均非空），防御跳过
            continue
        llm_usage_dao.record_usage(
            conn,
            day=usage["day"],
            provider=usage["provider"],
            model=usage["model"],
            stage=usage["stage"],
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )
        days.add(usage["day"])
        marked_at_rows.append((run["run_id"], usage["day"]))

    if marked_at_rows:
        from datetime import UTC, datetime

        marked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.executemany(
            "INSERT OR IGNORE INTO llm_usage_backfill (run_id, marked_at) VALUES (?,?)",
            [(rid, marked_at) for rid, _day in marked_at_rows],
        )
        conn.commit()
        logger.info("LLM 用量回填：%d 批，涉及 %d 天", len(marked_at_rows), len(days))

    return {"backfilled_days": len(days), "rows": len(marked_at_rows)}
