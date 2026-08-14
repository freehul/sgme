"""engine/health.py：提炼可观测性增强（v0.4 T15）。

职责：
- check_refinement_stalled：水位停滞检测（refined_at 距今超阈值 → stalled=True；
  T-12 起含 last_refined_seq 序号水位空转检测）
- check_seq_progression：序号水位推进检测（最近窗口内是否有 last_refined_seq 推进）
- check_llm_available：LLM 首链 provider 轻量 ping（/models 端点，5s 超时）
- check_heartbeat：综合心跳（LLM 可用 + 队列深度 + 最近提炼 + 停摆标记），异常发 anomaly_warn

设计依据：§3 / §11.1
- 异常时通过 signal.engine.publish('anomaly_warn', ...) 上报
- anomaly_warn 发布失败仅日志，不抛异常
- httpx 必须 trust_env=False（防 Clash 代理劫持 localhost 请求）
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger("sgme.engine.health")


def _now_dt() -> datetime:
    """当前 UTC aware datetime。"""
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    """解析 ISO 8601 时间戳为 aware datetime；失败返回 None。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 水位停滞检测 ----------

def check_seq_progression(
    session_conn: sqlite3.Connection,
    window_hours: int = 24,
) -> dict:
    """检测最近 window_hours 小时内提炼序号水位（last_refined_seq）是否推进。

    水位 = (refined_at, last_refined_seq) 二元组：refined_at 只证明"跑过
    refine 动作"，last_refined_seq 才证明"有提炼产出"。若窗口内反复 refine
    （refined_at 新鲜）却没有任何一行推进 seq（全部 ≤ 0），说明提炼在空转
    （如人为停摆/解析异常空转）——这是 refined_at 时间水位检测不到的停摆形态。

    - 窗口内无 refine 动作 → progressed=False（是否停摆交由时间水位判定）
    - 窗口内至少一行 last_refined_seq > 0 → progressed=True（有推进）
    - 窗口内全部 last_refined_seq ≤ 0 → progressed=False（空转）

    返回字段：progressed / window_refined_count / window_max_seq / window_hours
    """
    # 时间戳统一在 Python 侧按 ISO 解析比较（兼容 Z / +00:00 混合格式，
    # 避免 SQL 字符串比较在格式不一致时误判窗口边界）
    window_start = _now_dt() - timedelta(hours=window_hours)
    rows = session_conn.execute(
        "SELECT refined_at, last_refined_seq FROM raw_files "
        "WHERE refined_at IS NOT NULL"
    ).fetchall()
    window_rows = [
        r for r in rows
        if (dt := _parse_iso(r["refined_at"])) is not None and dt >= window_start
    ]
    window_refined_count = len(window_rows)
    window_max_seq = (
        max((r["last_refined_seq"] or 0) for r in window_rows)
        if window_rows else 0
    )
    progressed = window_refined_count > 0 and window_max_seq > 0
    return {
        "progressed": progressed,
        "window_refined_count": window_refined_count,
        "window_max_seq": window_max_seq,
        "window_hours": window_hours,
    }


def check_refinement_stalled(
    session_conn: sqlite3.Connection,
    threshold_hours: int = 24,
) -> dict:
    """检测提炼水位是否停滞。

    - 时间水位：查 raw_files 表 MAX(refined_at)，计算距今小时数，超阈值 → stalled
    - 序号水位（T-12 补齐）：窗口内（最近 threshold_hours 小时）有 refine 动作
      但无任何 last_refined_seq 推进（全部 ≤ 0）→ 提炼空转，同样视为停摆
    - 无任何 refined 记录视为停摆（stalled=True，stalled_hours=None）
    - 返回字段（契约冻结，不得增删）：stalled / last_refined_at / stalled_hours / threshold_hours
    """
    cur = session_conn.execute(
        "SELECT MAX(refined_at) AS last FROM raw_files WHERE refined_at IS NOT NULL"
    )
    row = cur.fetchone()
    last_refined = row["last"] if row else None

    if not last_refined:
        # 无提炼记录 → 视为停摆
        return {
            "stalled": True,
            "last_refined_at": None,
            "stalled_hours": None,
            "threshold_hours": threshold_hours,
        }

    last_dt = _parse_iso(last_refined)
    if last_dt is None:
        # 时间戳解析失败 → 视为停摆
        return {
            "stalled": True,
            "last_refined_at": last_refined,
            "stalled_hours": None,
            "threshold_hours": threshold_hours,
        }

    delta_hours = (_now_dt() - last_dt).total_seconds() / 3600.0
    stalled = delta_hours > threshold_hours

    # 序号水位（T-12）：窗口内有 refine 动作但全部未推进 seq → 空转停摆
    seq_info = check_seq_progression(session_conn, window_hours=threshold_hours)
    if seq_info["window_refined_count"] > 0 and not seq_info["progressed"]:
        stalled = True

    return {
        "stalled": stalled,
        "last_refined_at": last_refined,
        "stalled_hours": round(delta_hours, 2),
        "threshold_hours": threshold_hours,
    }


# ---------- LLM 可用性探测 ----------

def check_llm_available(cfg: dict, client: httpx.Client | None = None) -> dict:
    """LLM 首链 provider 轻量 ping（/models 端点）。

    - 取 cfg["llm"]["chains"]["refinement"][0] 首链 provider/model/base_url
    - 对 lm-studio / deepseek 调 /models 端点（httpx GET，trust_env=False，timeout=5s）
    - rule 兜底链视为不可用（available=False）
    - client 可注入（测试用 mock）；不传则用 sgme.llm.provider.make_client(timeout_s=5)
    - 返回字段：available / provider / model / error
    """
    try:
        chain = cfg["llm"]["chains"]["refinement"]
        head = chain[0] if chain else {}
    except (KeyError, IndexError, TypeError):
        return {
            "available": False,
            "provider": None,
            "model": None,
            "error": "refinement 链未配置",
        }

    provider = head.get("provider")
    model = head.get("model")
    base_url = head.get("base_url")

    # rule 兜底链视为不可用
    if not provider or provider == "rule" or not base_url:
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "error": "无可 ping 的 provider（rule 兜底或 base_url 缺失）",
        }

    own_client = client is None
    cli = client
    if own_client:
        from sgme.llm.provider import make_client
        cli = make_client(timeout_s=5.0)

    try:
        url = base_url.rstrip("/") + "/models"
        # 探测带首链鉴权（2026-08-08 修复）：deepseek 等云端 /models 需
        # Bearer 认证，此前不带头导致 health 恒报 401（误判 LLM 不可用）。
        # api_key_env 缺失（如 lm-studio 本地端点）或 key 为空 → 不带头，保持兼容。
        headers = None
        api_key_env = head.get("api_key_env")
        if api_key_env:
            api_key = os.environ.get(api_key_env)
            if api_key:
                headers = {"Authorization": f"Bearer {api_key}"}
        resp = cli.get(url, timeout=5.0, headers=headers)
        if resp.status_code == 200:
            return {
                "available": True,
                "provider": provider,
                "model": model,
                "error": None,
            }
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "error": f"HTTP {resp.status_code}",
        }
    except httpx.TimeoutException as e:
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "error": f"超时: {e}",
        }
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "error": f"连接错误: {e}",
        }
    except httpx.HTTPError as e:
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "error": f"HTTP 错误: {e}",
        }
    except Exception as e:
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "error": f"未知错误: {e}",
        }
    finally:
        if own_client and cli is not None:
            try:
                cli.close()
            except Exception:
                pass


# ---------- 综合心跳检查 ----------

def check_heartbeat(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict,
    client: httpx.Client | None = None,
) -> dict:
    """综合心跳：LLM 可用 + 队列深度 + 最近提炼时间 + 停摆标记。

    - 队列深度 = COUNT(*) FROM raw_files WHERE status='new'
    - 最近提炼时间 = MAX(refined_at) FROM raw_files WHERE refined_at IS NOT NULL
    - 调 check_refinement_stalled 和 check_llm_available
    - 任一异常（stalled=True 或 llm.available=False）→ 发布 anomaly_warn 信号
    - anomaly_warn 发布失败仅日志，不抛异常
    - 返回字段：llm / refinement / queue_depth / heartbeat_ok / stalled
    """
    llm_info = check_llm_available(cfg, client=client)
    refine_info = check_refinement_stalled(session_conn)

    # T-12：序号水位细节（供 anomaly_warn 载荷与调用方观测，非 /v1/health 契约字段）
    seq_info = check_seq_progression(
        session_conn, window_hours=refine_info.get("threshold_hours", 24)
    )
    seq_stalled = (
        seq_info["window_refined_count"] > 0 and not seq_info["progressed"]
    )

    # 队列深度
    cur_q = session_conn.execute(
        "SELECT COUNT(*) AS c FROM raw_files WHERE status='new'"
    )
    q_row = cur_q.fetchone()
    queue_depth = q_row["c"] if q_row else 0

    # 最近提炼时间
    cur_r = session_conn.execute(
        "SELECT MAX(refined_at) AS last FROM raw_files WHERE refined_at IS NOT NULL"
    )
    r_row = cur_r.fetchone()
    last_refined = r_row["last"] if r_row else None

    stalled = bool(refine_info.get("stalled"))
    llm_ok = bool(llm_info.get("available"))
    heartbeat_ok = (not stalled) and llm_ok

    # 异常时发布 anomaly_warn（不抛异常）
    if not heartbeat_ok:
        payload: dict[str, Any] = {
            "stalled": stalled,
            "stalled_hours": refine_info.get("stalled_hours"),
            "llm_available": llm_ok,
            "llm_error": llm_info.get("error"),
            "queue_depth": queue_depth,
            "last_refined_at": last_refined,
            "threshold_hours": refine_info.get("threshold_hours"),
            # T-12 新增：序号水位细节（窗口内空转 = 人为/异常停摆的可观测信号）
            "seq_stalled": seq_stalled,
            "window_refined_count": seq_info["window_refined_count"],
            "window_max_seq": seq_info["window_max_seq"],
            "window_hours": seq_info["window_hours"],
        }
        try:
            from sgme.signal import engine as signal_engine
            signal_engine.publish(
                event_type="anomaly_warn",
                source="health",
                payload=payload,
                mem_conn=mem_conn,
            )
        except Exception as e:
            logger.warning("anomaly_warn 发布失败（不阻塞）: %s", e)

    return {
        "llm": llm_info,
        "refinement": refine_info,
        "queue_depth": queue_depth,
        "heartbeat_ok": heartbeat_ok,
        "stalled": stalled,
        # T-12 新增（附加字段，历史契约字段不动）：序号水位空转标记 + 明细
        "seq_stalled": seq_stalled,
        "seq_progression": seq_info,
    }
