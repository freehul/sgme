"""Trae → SGME 桥接适配器（瘦桥接模式，对应 adapters/reasonix/bridge.py）。

Trae 通过 MCP 直连 SGME（9 个工具已暴露），不需要 hook。本模块提供：
- to_l0(): Trae session_memory jsonl → SGME L0 文本格式转换
- append_to_sgme(): L0 写入 SGME
- trigger_refine(): 触发批量提炼
- fetch_inject() / fetch_search(): 画像注入 + 语义检索

与 reasonix/bridge.py 的差异：
- 无 hook 逻辑（Trae 用 MCP，不走 SessionStart/SessionEnd hook）
- 数据源是 jsonl 摘要（intent/actions/outcome/learned），非原始会话日志
- 时间格式 "YYYY-MM-DD HH:MM:SS"（本地时间，转 UTC）
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

logger = logging.getLogger("sgme.trae")


def _load_env_file() -> None:
    """加载 adapters/trae/.env（install.py 写入的 SGME_AGENT_KEY）。

    只在环境变量未设置时填充（setdefault），不覆盖外部显式配置。
    """
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

# 与 adapters/reasonix 一致的端点与 Key 环境变量
_BASE_URL = os.environ.get("SGME_BASE_URL", "http://192.168.10.10:9910").rstrip("/")
_AGENT_KEY = os.environ.get("SGME_AGENT_KEY", "dev-agent-key-change-me")
_ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")
_AGENT_ID = os.environ.get("SGME_TRAE_AGENT_ID", "trae")

# Trae 本地时区（Asia/Shanghai, UTC+8）——message_summary_time 无时区信息，按本地时间解析
_TRAE_TZ = timezone(timedelta(hours=8))


# ---------- 时间归一化 ----------

def _norm_ts(ts: str | None) -> str:
    """时间戳归一化 → %Y-%m-%dT%H:%M:%SZ（UTC，与 SGME L0 同口径）。

    Trae 格式："2026-08-07 14:20:57"（本地时间 Asia/Shanghai）。
    """
    if not ts:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # Trae 格式："YYYY-MM-DD HH:MM:SS"（本地时间）
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TRAE_TZ)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        # 兜底：尝试 ISO 解析
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, AttributeError):
            return str(ts)  # 无法解析，原样返回


# ---------- 会话解析 ----------

def parse_trae_jsonl(path: Path) -> list[dict]:
    """解析 Trae session_memory jsonl → 记录列表。

    每行是一个 JSON 对象，包含 intent/actions/outcome/learned/message_summary_time。
    空行和解析失败的行跳过。
    """
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


# ---------- L0 格式 ----------

def to_l0(records: list[dict]) -> str:
    """Trae 记录列表 → SGME L0 消息块文本（`# {ts} user` / `## {ts} assistant`）。

    每条 record 转成一对 user/assistant 块：
    - user 块：intent（用户意图）
    - assistant 块：actions + outcome + learned（结构化输出，markdown 分节）

    格式与 sgme/raw/store.py parse_body_messages 对齐。
    空记录（无 intent 且无 outcome）跳过。
    """
    blocks: list[str] = []
    for r in records:
        ts = _norm_ts(r.get("message_summary_time"))
        intent = (r.get("intent") or "").strip()
        actions = r.get("actions") or []
        outcome = (r.get("outcome") or "").strip()
        learned = r.get("learned") or []

        if not intent and not outcome:
            continue  # 空记录跳过

        # user 块：意图
        if intent:
            blocks.append(f"# {ts} user\n{intent}")

        # assistant 块：结构化输出
        parts: list[str] = []
        if actions:
            parts.append("## 动作\n" + "\n".join(f"- {a}" for a in actions))
        if outcome:
            parts.append(f"## 结果\n{outcome}")
        if learned:
            parts.append("## 学到\n" + "\n".join(f"- {l}" for l in learned))
        if parts:
            blocks.append(f"## {ts} assistant\n" + "\n\n".join(parts))

    return "\n\n".join(blocks) + "\n" if blocks else ""


def APPEND_PAYLOAD(session_key: str, started_at: str, l0_text: str, agent_id: str) -> dict:
    """append 请求体（纯函数，便于测试）。"""
    return {
        "session_key": session_key,
        "started_at": started_at,
        "content": l0_text,
        "agent_id": agent_id,
    }


# ---------- SGME 调用 ----------

def _http() -> httpx.Client | None:
    if httpx is None:
        return None
    return httpx.Client(timeout=5.0, trust_env=False)  # trust_env=False 防 Clash 劫持 localhost


def append_to_sgme(l0_text: str, session_key: str, started_at: str, max_retries: int = 3) -> bool:
    """L0 写入 SGME（/v1/append，agent key）。

    遇 429 限流时按 retry_after_sec 等待后重试，最多 max_retries 次。
    """
    cli = _http()
    if cli is None:
        return False
    try:
        for attempt in range(max_retries + 1):
            r = cli.post(
                f"{_BASE_URL}/v1/append",
                json=APPEND_PAYLOAD(session_key, started_at, l0_text, _AGENT_ID),
                headers={"X-API-Key": _AGENT_KEY},
            )
            if r.status_code == 200:
                return True
            if r.status_code == 429 and attempt < max_retries:
                # 限流：读 retry_after_sec，无则默认 20s
                try:
                    retry_after = r.json().get("error", {}).get("details", {}).get("retry_after_sec", 20)
                except Exception:
                    retry_after = 20
                logger.info("trae append 限流，等待 %ss 后重试 (attempt %d/%d)", retry_after, attempt + 1, max_retries)
                time.sleep(retry_after + 1)
                continue
            logger.warning("trae append 失败: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return False
    except Exception as e:
        logger.warning("trae append 异常: %s", e)
        return False
    finally:
        cli.close()


def trigger_refine(limit: int = 50) -> bool:
    """触发提炼（/v1/admin/refine/trigger_async，admin key，fire-and-forget）。"""
    cli = _http()
    if cli is None:
        return False
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/admin/refine/trigger_async",
            json={"limit": limit},
            headers={"X-API-Key": _ADMIN_KEY},
        )
        return r.status_code in (200, 202)
    except Exception as e:
        logger.warning("trae refine trigger 异常: %s", e)
        return False
    finally:
        cli.close()


def fetch_inject(max_tokens: int = 800) -> list[dict]:
    """拉取 Tier0 用户画像（/v1/inject → blocks）。失败返回空列表。"""
    cli = _http()
    if cli is None:
        return []
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/inject",
            json={"mode": "daily", "max_tokens": max_tokens},
            headers={"X-API-Key": _AGENT_KEY},
        )
        if r.status_code != 200:
            return []
        return r.json().get("blocks", []) or []
    except Exception as e:
        logger.warning("trae inject 异常: %s", e)
        return []
    finally:
        cli.close()


def fetch_search(query: str, limit: int = 5) -> list[dict]:
    """语义检索（/v1/search → results）。失败返回空列表。"""
    cli = _http()
    if cli is None:
        return []
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/search",
            json={"query": query[:200], "scopes": ["memory"], "limit": limit},
            headers={"X-API-Key": _AGENT_KEY},
        )
        if r.status_code != 200:
            return []
        return r.json().get("results", []) or []
    except Exception as e:
        logger.warning("trae search 异常: %s", e)
        return []
    finally:
        cli.close()
