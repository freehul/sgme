"""skill_usage_dao.py：技能消费统计 DAO（T-174，2026-09-19）。

数据流：入口层埋点 → ``record_skill_usage()``（按日聚合 upsert 累加）
→ ``GET /v1/admin/skills/usage`` 查询。

与 ``api_usage_daily``（T-163，接口维度）**正交并存**，互不替代：
- api_usage_daily 回答「哪个端点 / MCP 工具被调用多少次」——name 是路由模板
  （``/v1/skills/{name}``），**具体技能名被归一化丢弃**；
- 本表回答「四级披露的哪一层被用、哪个技能被取用、谁在用」——skill 列保留技能名。

设计要点：
- 表 ``skill_usage_daily`` 见 ``db.py`` 的 ``SKILL_USAGE_DDL``；本模块是唯一读写方
- 写入方全静默（调用点 try/except）：统计是旁路，任何失败不得影响主链路
- 行粒度 = (day, layer, skill, caller)：一天一层一技能一调用方一行，upsert 累加；
  空间 O(天 × 层 × 技能 × 调用方)，保留 400 天由 ``prune_skill_usage`` 控制
- caller 取值同 api_usage_daily：agent_id / default / anonymous / unknown
- ``note`` 是「最近一次」诊断摘要（**非聚合**）：只允许两类——检索摘要
  （``q=…;hits=N;top=…``）与物化目标目录**类型**（windows/absolute/relative/…）。
  数据卫生：**真实路径不入库**（本机盘符路径、NAS 卷路径均为敏感值）

分层：本模块只做 SQL，不做业务判断（层/技能名提取在 operations 层，
caller 解析在入口层——同 T-163 分工）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

#: 四级披露的消费层（与 SGME-Skills管理模块设计-v0.2 §四级披露一一对应）
LAYERS = ("search", "digest", "get", "materialize")

#: note 长度上限（防不可控输入撑大聚合列；调用方已截断，此处再兜一层）
NOTE_MAX = 160


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def record_skill_usage(
    conn: sqlite3.Connection,
    layer: str,
    skill: str,
    caller: str,
    note: str | None = None,
    ip: str | None = None,
    *,
    ts: str | None = None,
) -> None:
    """记录一次技能消费（按 day+layer+skill+caller 聚合累加，幂等 upsert）。

    Args:
        conn: memory.db 连接（调用方持有；本函数自 commit）
        layer: ``search`` / ``digest`` / ``get`` / ``materialize``
        skill: 技能名；无单一技能名的调用（检索）用 ``-``
        caller: 调用方标识（agent_id / default / anonymous / unknown）
        note: 最近一次诊断摘要（可选；None 不覆盖已有值）
        ip: 来源 IP（可选，记录最近一次）
        ts: ISO UTC 时间戳（默认当前；测试可注入）
    """
    ts = ts or _now_iso()
    day = ts[:10]
    note_v = note[:NOTE_MAX] if isinstance(note, str) else None
    conn.execute(
        """
        INSERT INTO skill_usage_daily (day, layer, skill, caller, calls, last_ts, last_ip, note)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(day, layer, skill, caller)
        DO UPDATE SET calls = calls + 1,
                      last_ts = excluded.last_ts,
                      last_ip = COALESCE(excluded.last_ip, skill_usage_daily.last_ip),
                      note = COALESCE(excluded.note, skill_usage_daily.note)
        """,
        (day, layer, skill, caller, ts, ip, note_v),
    )
    conn.commit()


def query_skill_usage(
    conn: sqlite3.Connection,
    days: int = 30,
    layer: str | None = None,
    skill: str | None = None,
    caller: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """查询近 N 天技能消费统计：按 layer × skill × caller 聚合（次数降序）。

    ``note`` 取该组**最近一次**的值（相关子查询按 last_ts 取最新行）——
    它是诊断摘要而非聚合列。
    """
    since = (datetime.now(UTC) - timedelta(days=max(1, days))).date().isoformat()
    sql = """
        WITH agg AS (
            SELECT layer, skill, caller,
                   SUM(calls) AS calls,
                   MAX(last_ts) AS last_ts,
                   MAX(day) AS last_day
            FROM skill_usage_daily
            WHERE day >= ?
    """
    params: list = [since]
    if layer:
        sql += " AND layer = ?"
        params.append(layer)
    if skill:
        sql += " AND skill = ?"
        params.append(skill)
    if caller:
        sql += " AND caller = ?"
        params.append(caller)
    sql += """
            GROUP BY layer, skill, caller
        )
        SELECT agg.layer, agg.skill, agg.caller, agg.calls, agg.last_ts, agg.last_day,
               (SELECT s.note FROM skill_usage_daily s
                 WHERE s.layer = agg.layer AND s.skill = agg.skill AND s.caller = agg.caller
                 ORDER BY s.last_ts DESC LIMIT 1) AS note
        FROM agg
        ORDER BY agg.calls DESC, agg.layer, agg.skill, agg.caller
        LIMIT ?
    """
    params.append(max(1, int(limit)))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def prune_skill_usage(conn: sqlite3.Connection, keep_days: int = 400) -> int:
    """删除超过保留期（默认 400 天，对齐 api_usage_daily）的日行；返回删除行数。"""
    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).date().isoformat()
    cur = conn.execute("DELETE FROM skill_usage_daily WHERE day < ?", (cutoff,))
    conn.commit()
    return cur.rowcount or 0
