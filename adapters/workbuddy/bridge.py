r"""WorkBuddy → SGME 桥接适配器（瘦桥接模式，对标 adapters/trae/bridge.py）。

WorkBuddy 通过 MCP 直连 SGME（~/.workbuddy/mcp.json 配 X-API-Key），不需要 hook——
本模块只负责「批量/历史导入」的格式收敛与写入，与 adapters/trae 同形态：
- parse_workbuddy_jsonl(): WorkBuddy 会话 jsonl → 消息列表
- to_l0(): 消息列表 → SGME L0 文本格式
- append_to_sgme(): L0 写入 SGME
- trigger_refine(): 触发批量提炼
- fetch_inject() / fetch_search(): 画像注入 + 语义检索

与 trae/bridge.py 的差异：
- 无 hook 逻辑（WorkBuddy 用 MCP，不走 SessionStart/SessionEnd hook）
- 数据源是原始对话（role=user/assistant），非 Trae 的摘要（intent/actions/outcome）
- 时间戳是 epoch 毫秒 int（WorkBuddy 原生），归一化为 ISO UTC
- 项目目录编码：D:\Projects\SGME → d-Projects-SGME（WorkBuddy 专属）
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

logger = logging.getLogger("sgme.workbuddy")


def _load_env_file() -> None:
    """加载 adapters/workbuddy/.env（install.py / 手动写入的 SGME_AGENT_KEY）。

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

# 与 adapters/trae、adapters/reasonix 一致的端点与 Key 环境变量
_BASE_URL = os.environ.get("SGME_BASE_URL", "http://127.0.0.1:9910").rstrip("/")
_AGENT_KEY = os.environ.get("SGME_AGENT_KEY", "dev-agent-key-change-me")
_ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")
_AGENT_ID = os.environ.get("SGME_WORKBUDDY_AGENT_ID", "workbuddy")
# WorkBuddy 根目录（macOS/Linux 为 ~/.workbuddy，Windows 为 C:\Users\<u>\.workbuddy）
_WORKBUDDY_HOME = Path(os.environ.get("WORKBUDDY_HOME", Path.home() / ".workbuddy"))


# ---------- 项目目录编码 ----------

def encode_project_dir(path: str) -> str:
    r"""WorkBuddy 项目目录编码（与 WorkBuddy 自身一致）。

    D:\Projects\SGME                          → d-Projects-SGME
    D:\tmp                                    → d-tmp
    C:\Users\<user>\WorkBuddy\2026-08-12-01-32-00 → c-Users--user--WorkBuddy-2026-08-12-01-32-00
    /Users/leo/projects/foo                   → Users-leo-projects-foo
    """
    p = path.replace("/", "\\")
    drive = p[0].lower() if len(p) >= 2 and p[1] == ":" else ""
    rest = p[2:] if drive else p
    rest = re.sub(r"[^A-Za-z0-9-]", "-", rest).lstrip("-")
    return f"{drive}-{rest}" if drive else rest


# ---------- 时间归一化 ----------

def _norm_ts(ts) -> str:
    """时间戳归一化 → %Y-%m-%dT%H:%M:%SZ（UTC，与 SGME L0 同口径）。

    WorkBuddy timestamp 是 epoch 毫秒 int（如 1786461701510）；部分来源给 ISO 字符串。
    """
    if ts is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, (int, float)):
        # 毫秒级（>1e12）或秒级 epoch
        secs = ts / 1000.0 if ts > 1e12 else float(ts)
        return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (ValueError, AttributeError):
        return str(ts)


# ---------- 会话发现 ----------

def discover_sessions() -> list[Path]:
    """扫描 WorkBuddy 所有项目会话 jsonl。

    目录结构：~/.workbuddy/projects/<encoded-cwd>/<session-uuid>.jsonl
    """
    base = _WORKBUDDY_HOME / "projects"
    if not base.exists():
        return []
    return sorted(base.rglob("*.jsonl"), key=lambda p: str(p))


def find_session_file(session_id: str, cwd: str | None = None) -> Path | None:
    """按 sessionId 定位会话 jsonl（优先按 cwd 编码目录，全局扫描兜底）。"""
    if cwd:
        direct = _WORKBUDDY_HOME / "projects" / encode_project_dir(cwd) / f"{session_id}.jsonl"
        if direct.exists():
            return direct
    for cand in _WORKBUDDY_HOME.glob(f"**/{session_id}.jsonl"):
        return cand
    return None


def session_started_at(path: Path) -> int:
    """取会话最早一条消息的 epoch 毫秒（用于 --oldest 排序）。无消息返回 0。"""
    best = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict) or d.get("role") not in ("user", "assistant"):
                continue
            ts = d.get("timestamp")
            if isinstance(ts, (int, float)):
                if best is None or ts < best:
                    best = ts
    except OSError:
        return 0
    return best or 0


# ---------- 会话解析 ----------

# 非消息类 / 噪音块类型（不进入记忆提炼）
_NOISE_BLOCK_TYPES = {"reasoning", "thinking", "tool_use", "tool_result", "tool"}

# 剥离 user 文本中的 <system-reminder> 注入噪音
_REMINDER_RE = re.compile(r"<system-reminder\b.*?</system-reminder>", re.DOTALL)


def _block_text(block) -> str:
    if isinstance(block, dict):
        t = block.get("text")
        if isinstance(t, str):
            return t
    return ""


def parse_workbuddy_jsonl(path: Path) -> list[dict]:
    """解析 WorkBuddy 会话 jsonl → 消息列表（[{role, content, ts}]）。

    - type != 'message' 或 role 不在 {user,assistant} 的行跳过（file-history-snapshot 等）
    - user：取 input_text 块文本，剥离 <system-reminder> 注入噪音
    - assistant：取 output_text 块文本（丢弃 reasoning/thinking/tool 类噪音块）
    - 空文本消息跳过（无提炼价值）
    """
    msgs: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        role = d.get("role")
        if role not in ("user", "assistant"):
            continue
        ts = _norm_ts(d.get("timestamp"))
        content = d.get("content") or []
        texts: list[str] = []
        if isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") in _NOISE_BLOCK_TYPES:
                    continue
                t = _block_text(blk)
                if t:
                    texts.append(t)
        elif isinstance(content, str):
            texts.append(content)
        text = "\n".join(texts).strip()
        if role == "user":
            text = _REMINDER_RE.sub("", text).strip()
        if not text:
            continue
        msgs.append({"role": role, "content": text, "ts": ts})
    return msgs


# ---------- L0 格式 ----------

def to_l0(messages: list[dict]) -> str:
    """消息列表 → SGME L0 消息块文本（`# {ts} user` / `## {ts} assistant`）。

    格式与 sgme/raw/store.py parse_body_messages 对齐（与 trae/reasonix 同款）。
    """
    blocks: list[str] = []
    for m in messages:
        if m["role"] == "user":
            blocks.append(f"# {m['ts']} user\n{m['content']}")
        else:
            blocks.append(f"## {m['ts']} assistant\n{m['content']}")
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

def _http() -> "httpx.Client | None":
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
                logger.info("workbuddy append 限流，等待 %ss 后重试 (%d/%d)", retry_after, attempt + 1, max_retries)
                time.sleep(retry_after + 1)
                continue
            logger.warning("workbuddy append 失败: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return False
    except Exception as e:
        logger.warning("workbuddy append 异常: %s", e)
        return False
    finally:
        cli.close()


def trigger_refine(limit: int = 50, key: str | None = None) -> bool:
    """触发提炼（/v1/admin/refine/trigger_async，admin key，fire-and-forget）。

    注意鉴权分叉（SGME 现状）：
    - HTTP 端点 /v1/admin/refine/* 强制 require_admin_key（需 SGME_ADMIN_KEY）；
    - MCP 工具 refine_trigger 仅经 ApiKeyMiddleware 校验 is_agent，注册 agent key 即可。
    本函数走 HTTP 管理端点，故 key 缺省取 _ADMIN_KEY（= 环境变量 SGME_ADMIN_KEY）。
    若仅持有 WorkBuddy agent key，请用 MCP refine_trigger 工具触发提炼。
    """
    cli = _http()
    if cli is None:
        return False
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/admin/refine/trigger_async",
            json={"limit": limit},
            headers={"X-API-Key": key or _ADMIN_KEY},
        )
        return r.status_code in (200, 202)
    except Exception as e:
        logger.warning("workbuddy refine trigger 异常: %s", e)
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
        logger.warning("workbuddy inject 异常: %s", e)
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
        logger.warning("workbuddy search 异常: %s", e)
        return []
    finally:
        cli.close()
