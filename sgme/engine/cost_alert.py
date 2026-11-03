# -*- coding: utf-8 -*-
"""sgme/engine/cost_alert.py：LLM 成本告警线（设计 §7.2「6. 成本红线与告警」）。

地板规则（设计原文）：
- 基线：首月用估算表 ≈10M tokens/月（配置 baseline_tokens）；第二个月起可用
  「近 3 个完整月中位数 × 1.1」重设（auto_baseline）。
- 提示线 = 1.2× 基线、告警线 = 2× 基线；
- **只告警不停服、不自动切链**（半夜自动切 drop_batch 的代价远大于账单）；
- 告警进运维页 + 每日汇总，不走角色渠道（用户 08-24：拒绝自动推送）。

实现口径：
- 月度 token = llm_usage_daily 当前自然月 prompt+completion 合计；
- 告警写 signal_events（type=anomaly_warn, source=cost_alert），按
  (month, level) 去重——同月同档只发一次，避免每次记账都发；
- 读取侧 budget_status 无副作用，运维页直接用。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

logger = logging.getLogger("sgme.engine.cost_alert")

DEFAULT_COST_ALERT_CONFIG = {
    "enabled": True,
    "baseline_tokens": 10_000_000,   # 首月估算基线（设计 ≈10M）
    "warn_ratio": 1.2,
    "alert_ratio": 2.0,
    "auto_baseline": True,           # 满 3 个完整月后改用滚动中位数 ×1.1
    "auto_after_months": 3,
}


def _cfg(cfg: dict | None) -> dict:
    base = dict(DEFAULT_COST_ALERT_CONFIG)
    usr = (cfg or {}).get("cost_alert")
    if not isinstance(usr, dict):
        return base
    for k, v in usr.items():
        if k not in base or v is None:
            continue
        if k in ("enabled", "auto_baseline"):
            base[k] = bool(v)
        elif k in ("baseline_tokens", "auto_after_months"):
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv >= 0:
                base[k] = iv
        else:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                base[k] = fv
    return base


def _table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_usage_daily'"
    ).fetchone() is not None


def _month_tokens(conn: sqlite3.Connection, month: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c "
        "FROM llm_usage_daily WHERE day LIKE ?", (f"{month}%",)
    ).fetchone()
    return int(row["p"] or 0) + int(row["c"] or 0)


def _full_month_totals(conn: sqlite3.Connection, months: int) -> list[int]:
    """最近 N 个**完整**月（不含当前月）的 token 合计，新→旧。"""
    rows = conn.execute(
        "SELECT DISTINCT substr(day,1,7) m FROM llm_usage_daily ORDER BY m DESC"
    ).fetchall()
    current = datetime.now(UTC).strftime("%Y-%m")
    full = [r["m"] for r in rows if r["m"] and r["m"] < current][:months]
    return [_month_tokens(conn, m) for m in full]


def _median(values: list[int]) -> float:
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def budget_status(conn: sqlite3.Connection, cfg: dict | None = None) -> dict:
    """本月成本与告警档位（只读；运维页/告警共用）。"""
    c = _cfg(cfg)
    if not _table_exists(conn):
        return {
            "available": False, "enabled": c["enabled"], "month": None,
            "month_tokens": 0, "baseline_tokens": c["baseline_tokens"],
            "baseline_source": "config", "ratio": 0.0, "level": "none",
            "warn_ratio": c["warn_ratio"], "alert_ratio": c["alert_ratio"],
        }
    month = datetime.now(UTC).strftime("%Y-%m")
    tokens = _month_tokens(conn, month)
    baseline = int(c["baseline_tokens"])
    source = "config"
    if c["auto_baseline"]:
        totals = _full_month_totals(conn, int(c["auto_after_months"]))
        if len(totals) >= int(c["auto_after_months"]):
            baseline = int(round(_median(totals) * 1.1))
            source = "rolling_median"
    ratio = (tokens / baseline) if baseline > 0 else 0.0
    level = "none"
    if ratio >= float(c["alert_ratio"]):
        level = "alert"
    elif ratio >= float(c["warn_ratio"]):
        level = "warn"
    return {
        "available": True,
        "enabled": bool(c["enabled"]),
        "month": month,
        "month_tokens": tokens,
        "baseline_tokens": baseline,
        "baseline_source": source,
        "ratio": round(ratio, 4),
        "level": level,
        "warn_ratio": float(c["warn_ratio"]),
        "alert_ratio": float(c["alert_ratio"]),
    }


def evaluate_and_alert(conn: sqlite3.Connection, cfg: dict | None = None) -> dict:
    """评估告警并（按 月+档 去重）发布 anomaly_warn；**只告警不停服**。"""
    status = budget_status(conn, cfg)
    c = _cfg(cfg)
    status["alerted"] = False
    status["dedup_key"] = None
    status["write_error"] = None
    if not c["enabled"] or status["level"] == "none" or not status["available"]:
        return status
    dedup_key = f"cost:{status['month']}:{status['level']}"
    status["dedup_key"] = dedup_key
    try:
        exists = conn.execute(
            "SELECT 1 FROM signal_events WHERE type='anomaly_warn' AND source='cost_alert' "
            "AND payload LIKE ? LIMIT 1", (f"%{dedup_key}%",)
        ).fetchone()
        if exists:
            return status
        from sgme.signal import engine as signal_engine

        signal_engine.publish(
            "anomaly_warn",
            "cost_alert",
            {
                "dedup_key": dedup_key,
                "month": status["month"],
                "month_tokens": status["month_tokens"],
                "baseline_tokens": status["baseline_tokens"],
                "baseline_source": status["baseline_source"],
                "ratio": status["ratio"],
                "level": status["level"],
                "warn_ratio": status["warn_ratio"],
                "alert_ratio": status["alert_ratio"],
                "action": "只告警：不停服、不自动切链",
            },
            conn,
        )
        status["alerted"] = True
    except Exception as e:  # noqa: BLE001 - 告警失败不得影响记账主路径
        status["write_error"] = str(e)
        logger.warning("成本告警发布失败（不影响记账）: %s", e)
    return status
