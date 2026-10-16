"""storage/refine_dao.py：refine_runs 审计 DAO（#33 提示词版本管理）。

唯一写 refine_runs 表的入口。engine 只调 start/finish，不拼 SQL。
- start：批次开始（running）→ 返回 run_id
- finish：批次结束（ok/error）+ 计数 + action 分布
- summarize：按 (version, variant) 分组汇总（A/B 观测 / metrics 端点用）
- list_by_stage：明细（排障用）
- reap_stale_running：启动收敛超期 running 行（T-289 配套）

逐批记录语义（设计 §7）：L1 分块每块一条 run；L1.5 每候选批一条；
L2 每记忆批一条；Tier0 每次生成一条。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("sgme.data.refine_dao")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uuid() -> str:
    return str(uuid.uuid4())


# 悬挂 running 行的收敛阈值（秒）：started_at 早于该时长且仍为 running → 收敛为 error
# 取值依据：单批 LLM 调用实测 P95 远低于此；2× 批预算时长，宁可放宽不可误伤在途批次
STALE_RUNNING_SECONDS = 30 * 60


def _parse_iso(ts: str | None) -> datetime | None:
    """解析 `_now_iso()` 产出的 UTC 时间戳（宽松：兼容带小数秒与 +00:00 后缀）。"""
    if not ts:
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
        candidate_count: int | None = None,
        candidate_ids_hash: str | None = None,
        short_circuit_reason: str | None = None,
        provider: str | None = None,
    ) -> None:
        """结束批次：写计数 / action 分布 / 状态（ok|error）。

        usage（v0.5）：LLM 响应 usage dict {prompt_tokens, completion_tokens, total_tokens, ...}。

        L1.5 P1 F2（T1.7）候选留痕三列，None → NULL：
        - ``candidate_count`` / ``candidate_ids_hash`` **仅批路径承载**（本批候选数 +
          候选 memory_id 集排序拼接哈希 sha256[:16]，供「同候选集反查批次」）；
        - ``short_circuit_reason`` **仅非批路径独立短路记录承载**（如 rule_low_sim）；
          语义区分不依赖脆弱组合约定（v4 §二.4）。

        ``elapsed_ms``（ST-58/T-289）：批次真实墙钟耗时，由本函数依 ``started_at``
        计算写入——**调用点不传值**，防各处口径漂移。解析失败或 started_at 缺失时
        写 NULL（不影响 status 等主字段，也不因此抛错）。

        ``provider``（ST-58/T-289）：可选回填。T-289 把 `start()` 前移到 LLM 调用
        **之前**后，命中哪家提供商要跑完降级链才知道，故 start 时先写 ``"pending"``，
        由本形参在收尾时回填。用 ``COALESCE(?, provider)`` 语义——不传（None）即
        保持 start 时的值，老调用点零影响。
        """
        u = usage or {}
        elapsed_ms = None
        try:
            row = conn.execute(
                "SELECT started_at FROM refine_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            started = _parse_iso(row[0] if row else None)
            if started is not None:
                delta = datetime.now(timezone.utc) - started
                elapsed_ms = max(0, int(delta.total_seconds() * 1000))
        except Exception as exc:  # noqa: BLE001——耗时是旁路，不得影响主记账
            logger.warning("refine_runs.elapsed_ms 计算失败（run_id=%s）: %s", run_id, exc)
        conn.execute(
            """
            UPDATE refine_runs
            SET finished_at=?, memories_count=?, action_counts=?, status=?, error=?,
                prompt_tokens=?, completion_tokens=?, total_tokens=?,
                candidate_count=?, candidate_ids_hash=?, short_circuit_reason=?,
                elapsed_ms=?, provider=COALESCE(?, provider)
            WHERE run_id=?
            """,
            (_now_iso(), int(memories_count), json.dumps(action_counts or {}, ensure_ascii=False),
             status, error,
             u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"),
             candidate_count, candidate_ids_hash, short_circuit_reason,
             elapsed_ms, provider,
             run_id),
        )
        conn.commit()

    @staticmethod
    def reap_stale_running(
        conn: sqlite3.Connection,
        max_age_seconds: int = STALE_RUNNING_SECONDS,
        publish_anomaly: bool = True,
    ) -> int:
        """启动收敛：把超期仍为 `running` 的行置为 `error`。幂等，返回收敛行数。

        T-289 把 `start()` 前移到 LLM 调用之前后，未捕获异常会留下悬挂 running 行
        （调用点已用 `try/finally` 兜住绝大多数路径，本函数是**兜底的兜底**——
        覆盖进程被 kill / 容器重启等 finally 也来不及执行的场景）。

        语义：只改 status 并写 error 说明，**不补 finished_at**（保留「批次未正常
        收尾」的可辨识痕迹），elapsed_ms 保持 NULL（真实耗时不可知，不编造）。

        `publish_anomaly`：收敛到 >0 行时发 `anomaly_warn`（照 `edge_dao.py:285`
        先例，函数内延迟导入 `sgme.signal.engine`，避免 data → 上层静态依赖）。
        失败不影响收敛结果。
        """
        cutoff = _now_iso()
        try:
            cur = conn.execute(
                "SELECT run_id, started_at, stage FROM refine_runs WHERE status='running'"
            ).fetchall()
            stale: list[str] = []
            now = datetime.now(timezone.utc)
            for row in cur:
                started = _parse_iso(row[1])
                if started is None:
                    continue
                if (now - started).total_seconds() >= max_age_seconds:
                    stale.append(row[0])
            if not stale:
                return 0
            conn.executemany(
                "UPDATE refine_runs SET status='error', error=? WHERE run_id=?",
                [("批次未正常收尾（启动收敛，超期 running）", rid) for rid in stale],
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001——收敛失败不影响启动
            logger.warning("refine_runs 超期 running 收敛失败: %s", exc)
            return 0

        logger.warning(
            "refine_runs 启动收敛：%d 条超期 running 行置为 error（阈值 %ds，检查时刻 %s）",
            len(stale), max_age_seconds, cutoff,
        )
        if publish_anomaly:
            try:
                from sgme.signal import engine as signal_engine
                signal_engine.publish(
                    "anomaly_warn", "refine_stale_running",
                    {"count": len(stale), "max_age_seconds": max_age_seconds,
                     "checked_at": cutoff},
                    conn,
                )
            except Exception as exc:  # noqa: BLE001——告警发布失败不影响收敛结果
                logger.warning("refine_runs 超期收敛告警发布失败: %s", exc)
        return len(stale)

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
