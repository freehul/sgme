"""engine/persona_monthly.py：月度人格校准（ST-35 T-100）。

SGME 内部计时的月度 LLM 全局分析（2026-08-25 用户定案：不放 agent 定时任务）。
与 Dream 同款架构：daemon 线程 + ensure_scheduler 幂等启动 + 执行锁防重入。

流程
----
1. 到期判断：persona_state.last_run 与当前月份比较（跨月即触发，重启补跑）
2. 输入收集：traits 表全量 + 当月新增记忆摘要（结构化，控 token）
3. LLM 分析：复用 call_with_fallback 降级链（免费链），输出 JSON：
   {mbti: "INFJ", traits: [{dimension, value, action, confidence}], report: "..."}
4. 落库：报告 persona_reports + MBTI 锚点 user_mbti(source=llm_monthly)
   + 特质置信度更新/确认
5. 变化检测：连续 2 个月同向变化才升级为「已确认变化」推入信号模块
   （单月只记 observation，防 MBTI 式重测误报）

配置（config persona.monthly 段）：
    enabled: true          # 总开关（与 persona.enabled 双层）
    schedule_day: 1        # 每月几号跑（1-28，本地时区）
    schedule_time: "03:30" # 当日 HH:MM（低峰）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from sgme.data import persona_dao

logger = logging.getLogger("sgme.engine.persona_monthly")

RUN_LOCK = threading.Lock()

_scheduler_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop: threading.Event | None = None

# 月度分析提示词模板（输出 JSON；措辞用「倾向」不用判决）
_PROMPT_TEMPLATE = """你是人格洞察分析师。基于用户的记忆引擎数据做一次月度人格倾向校准。

# 当前特质累积（dimension / value / confidence / evidence_count）
{{traits}}

# 近期新增记忆摘要（当月）
{{recent_memories}}

# 用户自报 MBTI 历史（参考锚点，非强制结论）
{{mbti_history}}

# 任务
1. mbti：给出本月的娱乐向 MBTI 判断（4 字母）。证据不足时沿用最近自报值。
2. traits：对现有特质逐条给出 action——confirm（维持）/ adjust（微调 confidence ±0.05~0.15）/ supersede（出现对立倾向且证据充分，旧特质将被标记替代）。禁止凭空创建新维度。
3. report：300 字以内的中文月度洞察报告。措辞用「倾向」「迹象」，禁止标签式判决（不说"你就是XX人"）。
4. changes：本月观察到的变化方向列表 [{dimension, from, to}]，无变化给空数组。

# 输出要求
只输出一个 JSON 对象，无 markdown 围栏、无说明文字：
{"mbti": "XXXX", "traits": [{"dimension": "...", "value": "...", "action": "confirm|adjust|supersede", "confidence_delta": 0.0}], "report": "...", "changes": []}
"""


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _current_period() -> str:
    return _now_local().strftime("%Y-%m")


def is_due(mem_conn: sqlite3.Connection) -> bool:
    """到期判断：persona_state.last_run 不等于当前月份 = 到期（含从未跑过）。"""
    return persona_dao.get_state(mem_conn, "last_run") != _current_period()


def run_calibration(
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    client=None,
) -> dict[str, Any]:
    """执行一次月度校准（阻塞）。锁被占用返回 running。

    Returns:
        {status, period, report_id?, mbti?, changes?, error?}
    """
    if not RUN_LOCK.acquire(blocking=False):
        return {"status": "running", "message": "人格校准执行中（防重入）"}
    try:
        return _run_locked(mem_conn, cfg, client=client)
    finally:
        RUN_LOCK.release()


def _run_locked(
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    client=None,
) -> dict[str, Any]:
    period = _current_period()
    # 先落 last_run 防止失败后无限重试烧 token（失败可手动重触发清状态）
    persona_dao.set_state(mem_conn, "last_run", period)

    try:
        from sgme.config import load_llm_config
        llm_cfg = load_llm_config()
    except Exception as e:
        logger.warning("LLM 配置读取失败，跳过本次校准: %s", e)
        persona_dao.set_state(mem_conn, "last_run", "")
        return {"status": "error", "error": f"llm_config_unavailable: {e}"}

    # 收集输入
    traits = persona_dao.list_traits(mem_conn, limit=200)
    recent = mem_conn.execute(
        "SELECT content FROM memories WHERE updated_at >= ? AND status='active'",
        (period + "-01",),
    ).fetchall()
    mbti_hist = persona_dao.get_mbti_history(mem_conn)

    prompt = (
        _PROMPT_TEMPLATE
        .replace("{{traits}}", json.dumps(traits, ensure_ascii=False)[:6000])
        .replace(
            "{{recent_memories}}",
            json.dumps([dict(r) for r in recent[:80]], ensure_ascii=False)[:8000],
        )
        .replace(
            "{{mbti_history}}",
            json.dumps(mbti_hist[-6:], ensure_ascii=False),
        )
    )

    try:
        from sgme.refinery.extract import extract
        data = extract(
            prompt,
            {"mbti": str, "traits": list, "report": str},
            llm_cfg,
            client=client,
        )
    except Exception as e:
        logger.warning("月度校准 LLM 失败: %s", e)
        _publish_signal(mem_conn, "anomaly_warn", {
            "module": "persona_monthly", "period": period, "error": str(e),
        })
        return {"status": "error", "period": period, "error": str(e)}

    # 落库：报告 + MBTI + 特质动作
    mbti_type = (data.get("mbti") or "").strip().upper()
    valid_mbti = persona_dao.validate_mbti(mbti_type)
    if valid_mbti:
        persona_dao.add_mbti_record(
            mem_conn, mbti_type, source="llm_monthly",
            note=f"{period} 月度校准",
        )
    else:
        mbti_type = ""

    trait_changes: list[dict] = []
    for t in data.get("traits") or []:
        dim, val = t.get("dimension"), t.get("value")
        action = t.get("action")
        if not dim or not val or action == "confirm":
            continue
        row = mem_conn.execute(
            """SELECT trait_id FROM persona_traits
               WHERE dimension=? AND value=? AND status='active'""",
            (dim, val),
        ).fetchone()
        if row is None:
            continue
        delta = float(t.get("confidence_delta") or 0)
        conn_row = mem_conn.execute(
            "SELECT confidence FROM persona_traits WHERE trait_id=?",
            (row["trait_id"],),
        ).fetchone()
        new_conf = min(max(conn_row["confidence"] + delta, 0.05), 1.0)
        mem_conn.execute(
            "UPDATE persona_traits SET confidence=?, source='llm_monthly', updated_at=datetime('now') WHERE trait_id=?",
            (round(new_conf, 4), row["trait_id"]),
        )
        trait_changes.append({"dimension": dim, "value": val, "action": action})
    mem_conn.commit()

    report = persona_dao.save_report(
        mem_conn, period, data.get("report") or "",
        mbti_result=mbti_type or None,
        trait_changes=data.get("changes") or [],
    )

    # 变化检测（防误报）：连续两个月同向才推「变化」信号，否则记 observation
    confirmed_changes = _detect_confirmed_changes(mem_conn, period, data.get("changes") or [])
    for ch in confirmed_changes:
        _publish_signal(mem_conn, "memory_updated", {
            "module": "persona_monthly", "period": period,
            "type": "persona_change_confirmed", **ch,
        })

    logger.info(
        "月度人格校准完成 period=%s mbti=%s changes=%d confirmed=%d",
        period, mbti_type or "-", len(trait_changes), len(confirmed_changes),
    )
    return {
        "status": "done",
        "period": period,
        "report_id": report["report_id"],
        "mbti": mbti_type or None,
        "observations": len(data.get("changes") or []),
        "confirmed_changes": len(confirmed_changes),
    }


def _detect_confirmed_changes(
    mem_conn: sqlite3.Connection,
    period: str,
    current_changes: list[dict],
) -> list[dict]:
    """连续 2 个月同向的变化才确认为真变化。

    规则：本期 changes 中某 (dimension, from→to)，若上月报告的 trait_changes
    含相同条目 → 确认；否则仅为本期 observation。
    """
    if not current_changes:
        return []
    reports = persona_dao.list_reports(mem_conn, limit=2)
    # 倒数第二份报告（最近一份是本期刚落的）
    prev = reports[1] if len(reports) > 1 else None
    if prev is None:
        return []
    prev_set = {
        (c.get("dimension"), c.get("from"), c.get("to"))
        for c in prev["trait_changes"]
        if isinstance(c, dict)
    }
    confirmed = []
    for c in current_changes:
        key = (c.get("dimension"), c.get("from"), c.get("to"))
        if key in prev_set:
            confirmed.append(c)
    return confirmed


def _publish_signal(
    mem_conn: sqlite3.Connection, event_type: str, payload: dict
) -> None:
    try:
        from sgme.signal import engine as signal_engine
        signal_engine.publish(
            event_type=event_type, source="persona_monthly",
            payload=payload, mem_conn=mem_conn,
        )
    except Exception as e:
        logger.warning("信号发布失败（不阻塞）: %s", e)


# ======================================================================
# 定时器（Dream 同款：daemon 线程 + 每小时醒一次检查是否到 schedule_day/time）
# ======================================================================

def _seconds_until_next_run(schedule_day: int, schedule_time: str) -> float:
    try:
        h, _m = schedule_time.strip().split(":", 1)
        int(h)  # 格式校验
    except Exception:
        logger.warning("persona monthly schedule_time 格式非法（1 小时后重试）: %r", schedule_time)
    # 月度任务粗粒度即可：每小时醒一次轮询
    return 3600.0


def _scheduler_loop(cfg: dict, stop: threading.Event, data_dir: Path | None) -> None:
    while not stop.is_set():
        try:
            persona_cfg = (cfg.get("persona") or {}).get("monthly") or {}
            if not persona_cfg.get("enabled"):
                if stop.wait(3600):
                    break
                continue
            day = int(persona_cfg.get("schedule_day", 1) or 1)
            h, m = (persona_cfg.get("schedule_time", "03:30") or "03:30").split(":", 1)
            now = _now_local()
            due_time = (
                now.day >= day and now.hour >= int(h) and now.minute >= int(m)
            )
            if not due_time:
                if stop.wait(1800):
                    break
                continue

            # 到点执行（自建连接隔离，run_dream_safe 同款）
            from sgme.data import db as db_mod
            d = Path(data_dir) if data_dir else __import__("sgme.config", fromlist=["DATA_DIR"]).DATA_DIR
            mem_conn, _, _ = db_mod.init_databases(d)
            try:
                if is_due(mem_conn):
                    result = run_calibration(mem_conn, cfg)
                    logger.info("月度人格校准触发: %s", result.get("status"))
            finally:
                db_mod.close(mem_conn)
            stop.wait(86400 - 1800)  # 跑过后当天不再重复检查
        except Exception as e:
            logger.exception("persona monthly scheduler 异常（继续循环）: %s", e)
            if stop.wait(3600):
                break


def ensure_scheduler(cfg: dict, data_dir: str | Path | None = None) -> None:
    """幂等启动常驻定时器线程。"""
    global _scheduler_thread, _scheduler_stop
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return
        _scheduler_stop = threading.Event()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            args=(cfg, _scheduler_stop, Path(data_dir) if data_dir else None),
            daemon=True,
            name="sgme-persona-scheduler",
        )
        _scheduler_thread.start()
        logger.info("人格月度校准定时器已启动")


def stop_scheduler(timeout: float = 5.0) -> bool:
    global _scheduler_thread, _scheduler_stop
    with _scheduler_lock:
        if _scheduler_thread is None or not _scheduler_thread.is_alive():
            _scheduler_thread = None
            return False
        if _scheduler_stop is not None:
            _scheduler_stop.set()
        _scheduler_thread.join(timeout)
        if not _scheduler_thread.is_alive():
            _scheduler_thread = None
            return True
        return False
