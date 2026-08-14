"""storage/refine_dao.py：refine_runs 审计 DAO（#33 提示词版本管理）。

唯一写 refine_runs 表的入口。engine 只调 start/finish，不拼 SQL。
- start：批次开始（running）→ 返回 run_id
- finish：批次结束（ok/error）+ 计数 + action 分布
- summarize：按 (version, variant) 分组汇总（A/B 观测 / metrics 端点用）
- list_by_stage：明细（排障用）

逐批记录语义（设计 §7）：L1 分块每块一条 run；L1.5 每候选批一条；
L2 每记忆批一条；Tier0 每次生成一条。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uuid() -> str:
    return str(uuid.uuid4())


class RefineRunRecorder:
    """refine_runs 批次审计（纯 storage 层，conn 由调用方传入）。"""

    # ---------- 写 ----------

    @staticmethod
    def start(
        conn: sqlite3.Connection,
        file_id: str,
        stage: str,
        version: str,
        variant: str | None,
        provider: str,
        bucket_key: str,
    ) -> str:
        """记录批次开始（status=running），返回 run_id。"""
        run_id = _uuid()
        conn.execute(
            """
            INSERT INTO refine_runs
              (run_id, file_id, stage, version, variant, provider, bucket_key,
               started_at, status)
            VALUES (?,?,?,?,?,?,?,?, 'running')
            """,
            (run_id, file_id, stage, version, variant, provider, bucket_key, _now_iso()),
        )
        conn.commit()
        return run_id

    @staticmethod
    def finish(
        conn: sqlite3.Connection,
        run_id: str,
        memories_count: int,
        action_counts: dict,
        status: str,
        error: str | None = None,
        usage: dict | None = None,
    ) -> None:
        """结束批次：写计数 / action 分布 / 状态（ok|error）。

        usage（v0.5）：LLM 响应 usage dict {prompt_tokens, completion_tokens, total_tokens, ...}。
        """
        u = usage or {}
        conn.execute(
            """
            UPDATE refine_runs
            SET finished_at=?, memories_count=?, action_counts=?, status=?, error=?,
                prompt_tokens=?, completion_tokens=?, total_tokens=?
            WHERE run_id=?
            """,
            (_now_iso(), int(memories_count), json.dumps(action_counts or {}, ensure_ascii=False),
             status, error,
             u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"),
             run_id),
        )
        conn.commit()

    # ---------- 读 ----------

    @staticmethod
    def list_by_stage(
        conn: sqlite3.Connection,
        stage: str,
        since: str | None = None,
    ) -> list[dict]:
        """列出某 stage 的 run 明细（可选 since 过滤 started_at）。"""
        sql = "SELECT * FROM refine_runs WHERE stage=?"
        params: list = [stage]
        if since:
            sql += " AND started_at >= ?"
            params.append(since)
        sql += " ORDER BY started_at DESC, run_id"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    @staticmethod
    def summarize(
        conn: sqlite3.Connection,
        stage: str,
        since: str | None = None,
    ) -> dict:
        """按 (version, variant) 分组汇总（A/B 观测）。

        返回：
          {
            "stage": str,
            "since": str | None,
            "groups": [
              {
                "version": "v002", "variant": "A",
                "runs": N, "error_runs": M,
                "memories_count": total,
                "avg_priority": float | None,   # 来自 memories.prompt_version（同版本 A/B 共享）
                "memories_rows": int,           # memories 表按版本计数
                "action_dist": {"store": n, ...}
              }, ...
            ]
          }
        """
        where = "stage=?"
        params: list = [stage]
        if since:
            where += " AND started_at >= ?"
            params.append(since)
        rows = conn.execute(
            f"""
            SELECT version, variant, status, memories_count, action_counts
            FROM refine_runs WHERE {where} ORDER BY version, variant
            """,
            params,
        ).fetchall()

        groups: dict[tuple[str, str | None], dict] = {}
        for r in rows:
            key = (r["version"], r["variant"])
            g = groups.setdefault(key, {
                "version": r["version"],
                "variant": r["variant"],
                "runs": 0,
                "error_runs": 0,
                "memories_count": 0,
                "action_dist": {},
            })
            g["runs"] += 1
            if r["status"] == "error":
                g["error_runs"] += 1
            g["memories_count"] += int(r["memories_count"] or 0)
            try:
                dist = json.loads(r["action_counts"] or "{}")
            except (json.JSONDecodeError, TypeError):
                dist = {}
            for k, v in dist.items():
                g["action_dist"][k] = g["action_dist"].get(k, 0) + int(v)

        # 记忆侧统计：memories.prompt_version = "<stage>:<version>"（变体在 refine_runs 查）
        mem_rows = conn.execute(
            """
            SELECT prompt_version, COUNT(*) AS cnt, AVG(priority) AS avg_pri
            FROM memories
            WHERE prompt_version IS NOT NULL AND prompt_version LIKE ?
            GROUP BY prompt_version
            """,
            (f"{stage}:%",),
        ).fetchall()
        mem_by_version: dict[str, dict] = {}
        for r in mem_rows:
            version = str(r["prompt_version"]).split(":", 1)[1]
            mem_by_version[version] = {
                "memories_rows": int(r["cnt"]),
                "avg_priority": round(float(r["avg_pri"]), 2) if r["avg_pri"] is not None else None,
            }

        for g in groups.values():
            m = mem_by_version.get(g["version"], {})
            g["memories_rows"] = m.get("memories_rows", 0)
            g["avg_priority"] = m.get("avg_priority")

        return {
            "stage": stage,
            "since": since,
            "groups": [groups[k] for k in sorted(groups, key=lambda x: (x[0], x[1] or ""))],
        }

# ---------- 浏览分页（0.8 T-15 / 契约 §5.5） ----------

#: 契约 §5.5.2 的响应字段（顺序即响应键序）。``variant`` / ``bucket_key``
#: 属 A/B 实验内部字段，契约未列出，故不外泄。
_BROWSE_RUN_COLUMNS: tuple[str, ...] = (
    "run_id", "file_id", "stage", "version", "provider", "status", "error",
    "started_at", "finished_at", "memories_count", "action_counts",
    "prompt_tokens", "completion_tokens", "total_tokens",
)


def list_runs_page(
    conn: sqlite3.Connection,
    *,
    page: int = 1,
    limit: int = 50,
    stage: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[list[dict], int]:
    """提炼记录分页查询（契约 §5.5；WebUI 提炼监控 / SCSM 健康观测）。

    与 ``RefineRunRecorder.list_by_stage`` 的分工：那个是排障用的单 stage 全量拉取
    （无分页、无 total、SELECT *）；本函数面向监控界面——需要分页、总数与
    契约裁剪后的字段集，故独立成函数而非改动既有只读方法（既有调用方零影响）。

    **刻意不设 status 缺省过滤**：提炼监控的价值恰在于看见 error 与 running，
    默认隐藏会让异常静默。这与 memories/scenes「默认仅 active」是相反的产品取向。

    排序固定 ``started_at DESC``（契约 §5.5.1 未开放 sort 参数），
    追加 ``run_id`` 决胜键保证同一时间戳下分页稳定。

    Args:
        conn: memory.db 连接（refine_runs 在 memory.db）。
        page: 页码（≥ 1，调用方已校验）。
        limit: 页大小（1..200，调用方已校验）。
        stage: ``l1_extraction`` / ``l1_conflict`` / ``l2_scene`` / ``tier0_summary``；
            None 不过滤。
        status: ``running`` / ``ok`` / ``error``；None 不过滤。
        since / until: 作用于 ``started_at`` 的闭区间边界。

    Returns:
        ``(items, total)``。``action_counts`` 按库内原始 JSON **字符串**透传
        （契约 §5.5.2 示例即字符串形态，不在此反序列化）。
    """
    where: list[str] = []
    params: list = []
    if stage:
        where.append("stage = ?")
        params.append(stage)
    if status:
        where.append("status = ?")
        params.append(status)
    if since:
        where.append("started_at >= ?")
        params.append(since)
    if until:
        where.append("started_at <= ?")
        params.append(until)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM refine_runs{where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT {', '.join(_BROWSE_RUN_COLUMNS)} FROM refine_runs{where_sql}
        ORDER BY started_at DESC, run_id DESC
        LIMIT ? OFFSET ?
        """,
        params + [int(limit), max(0, (int(page) - 1) * int(limit))],
    ).fetchall()

    return [dict(r) for r in rows], int(total)
