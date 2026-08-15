"""Reasonix → SGME 桥接适配器（hooks 专用，对应 adapters/hermes 的瘦桥接模式）。

在 Reasonix 的 `.reasonix/settings.json` 配两个 hook（事件驱动，不靠 LLM 自主触发）：
  SessionStart → bridge.py --start   （PR#2：注入 SGME 画像 + 相关记忆）
  SessionEnd   → bridge.py --end     （PR#1：导出 Reasonix 会话到 SGME L0 + 触发提炼）

hook 命令示例（Windows）：
  <项目根>/.venv/Scripts/python.exe <项目根>/adapters/reasonix/bridge.py --end

stdin 收到 Reasonix hook payload（一行 JSON：{event, cwd, sessionId, ...}）。
故障隔离：任何异常只写 stderr 并 exit 0——绝不阻塞 Reasonix 会话生命周期。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

logger = logging.getLogger("sgme.reasonix")

def _load_env_file() -> None:
    """加载 adapters/reasonix/.env（install.py 写入的 SGME_AGENT_KEY）。

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

# 与 adapters/hermes 一致的端点与 Key 环境变量
_BASE_URL = os.environ.get("SGME_BASE_URL", "http://192.168.10.10:9910").rstrip("/")
_AGENT_KEY = os.environ.get("SGME_AGENT_KEY", "dev-agent-key-change-me")
_ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")
_AGENT_ID = os.environ.get("SGME_REASONIX_AGENT_ID", "reasonix")

# Reasonix 会话目录根（Windows %APPDATA%\\reasonix\\projects）
_REASONIX_HOME = Path(os.environ.get("REASONIX_HOME", "")) if os.environ.get("REASONIX_HOME") else None

# 内部噪音工具名（Reasonix 运行时标记，非真实工具调用）
_LOCAL_ONLY_NAME = "__reasonix_local_only__"


# ---------- 项目目录编码 ----------

def encode_project_dir(path: str) -> str:
    """Reasonix 项目目录编码：小写 + 非 [a-z0-9-] 字符转 '-'.

    实测（v1.19.1）：D:\\Projects\\rx-hook-test → d--projects-rx-hook-test
    """
    return re.sub(r"[^a-z0-9-]", "-", path.lower())


def _reasonix_projects_root() -> Path:
    """Reasonix 全局 projects 根目录（Windows: %APPDATA%\\reasonix\\projects）。"""
    if _REASONIX_HOME:
        return _REASONIX_HOME / "projects"
    return Path(os.environ.get("APPDATA", "")) / "reasonix" / "projects"


def find_session_file(session_id: str, cwd: str) -> Path | None:
    """按 sessionId 定位会话 jsonl 文件。

    优先按项目目录编码规则（cwd → 目录名）；编码不匹配时全局扫描兜底。
    """
    projects_root = _reasonix_projects_root()
    if not projects_root.exists():
        return None

    # 优先：编码规则
    encoded = encode_project_dir(cwd)
    direct = projects_root / encoded / "sessions" / f"{session_id}.jsonl"
    if direct.exists():
        return direct

    # 兜底：全局扫描（sessionId 全局唯一）
    for cand in projects_root.glob(f"*/sessions/{session_id}.jsonl"):
        return cand
    return None


# ---------- 会话解析 ----------

def _norm_ts(ts: str | int | float | None) -> str:
    """时间戳归一化 → %Y-%m-%dT%H:%M:%SZ（UTC，与 SGME L0 同口径）。

    实测 Reasonix createdAt 是 epoch 毫秒 int（如 1785755032100）；部分版本给 ISO 字符串。
    """
    if ts is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, (int, float)):
        # 毫秒级（>1e12）或秒级 epoch
        secs = ts / 1000.0 if ts > 1e12 else float(ts)
        return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except ValueError:
        return ts


def _tool_msg_key(d: dict) -> str:
    """tool 消息去重指纹：优先 tool_call_id；缺失时 name+content 哈希兜底。

    ST-23③ tool 重复治理（2026-08-11 实锤：同一 tool 事件被 Reasonix jsonl
    源数据记 4 次）——与 adapters/hermes 的 _msg_key 同款方案（tool 用调用
    ID 判重，无 ID 退内容指纹）。
    """
    tid = d.get("tool_call_id") or ""
    if tid:
        return f"tool:{tid}"
    return f"tool:{d.get('name', 'tool')}:{hash(str(d.get('content') or ''))}"


def parse_session_file(path: Path) -> list[dict]:
    """解析 Reasonix 会话 jsonl → 消息列表（[{role, content, ts, name?, tool_call_id?}]）。

    - system 行：过滤（系统提示词噪音）
    - __reasonix_local_only__ 行：过滤（Reasonix 内部标记）
    - user：优先 raw_content（去掉 reasoning-language 注入前缀），ts 取 createdAt
    - assistant：取 content（reasoning_content 丢弃），ts 继承上一条
    - tool：取 content + name + tool_call_id（如有），ts 继承上一条；
      同一 tool 事件（tool_call_id / name+content 指纹相同）只保留一条（ST-23③）
    """
    msgs: list[dict] = []
    seen_tools: set[str] = set()
    last_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = d.get("role", "")
        if role == "system":
            continue
        if role == "tool" and d.get("name") == _LOCAL_ONLY_NAME:
            continue
        if role == "user":
            content = d.get("raw_content") or d.get("content") or ""
            last_ts = _norm_ts(d.get("createdAt"))
        elif role == "assistant":
            content = d.get("content") or ""
        elif role == "tool":
            content = d.get("content") or ""
            if not content.strip():
                continue  # 空 tool 输出无提炼价值
            key = _tool_msg_key(d)
            if key in seen_tools:
                continue  # 同一 tool 事件重复记录（ST-23③），只导一次
            seen_tools.add(key)
            item: dict = {"role": "tool", "content": content,
                          "name": d.get("name", "tool"), "ts": last_ts}
            tid = d.get("tool_call_id")
            if tid:
                item["tool_call_id"] = tid
            msgs.append(item)
            continue
        else:
            continue
        if not content.strip():
            continue  # 空消息（如仅 reasoning 的 assistant 行）无提炼价值
        msgs.append({"role": role, "content": content, "ts": last_ts})
    return msgs


# ---------- L0 格式 ----------

def to_l0(messages: list[dict]) -> str:
    """消息列表 → SGME L0 消息块文本（`# {ts} user` / `## {ts} assistant|tool`）。

    tool 块首行 `**tool**: {name}`（与 sgme/raw/store.py parse_body_messages 对齐）。
    """
    blocks: list[str] = []
    for m in messages:
        if m["role"] == "user":
            blocks.append(f"# {m['ts']} user\n{m['content']}")
        elif m["role"] == "tool":
            blocks.append(f"## {m['ts']} tool\n**tool**: {m.get('name', 'tool')}\n{m['content']}")
        else:
            blocks.append(f"## {m['ts']} assistant\n{m['content']}")
    return "\n\n".join(blocks) + "\n"


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


def append_to_sgme(l0_text: str, session_key: str, started_at: str) -> bool:
    """L0 写入 SGME（/v1/append，agent key）。"""
    cli = _http()
    if cli is None:
        return False
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/append",
            json=APPEND_PAYLOAD(session_key, started_at, l0_text, _AGENT_ID),
            headers={"X-API-Key": _AGENT_KEY},
        )
        return r.status_code == 200
    except Exception as e:
        logger.warning("reasonix append 异常: %s", e)
        return False
    finally:
        cli.close()


def trigger_refine() -> bool:
    """触发提炼（/v1/admin/refine/trigger_async，admin key，fire-and-forget）。"""
    cli = _http()
    if cli is None:
        return False
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/admin/refine/trigger_async",
            json={"limit": 50},
            headers={"X-API-Key": _ADMIN_KEY},
        )
        return r.status_code in (200, 202)
    except Exception as e:
        logger.warning("reasonix refine trigger 异常: %s", e)
        return False
    finally:
        cli.close()


# ---------- 增量导出游标（ST-23③，2026-08-11） ----------

# 本地持久化游标：{session_key: {"exported": int, "last_started_at": str}}
# 与 .env 同级（适配器本地运行时状态，不入 git）
_CURSOR_FILE = Path(__file__).resolve().parent / ".export_cursor.json"


def _load_cursor() -> dict:
    """加载导出游标；文件缺失/损坏 → 空 dict（不抛异常，走兜底种子）。"""
    if not _CURSOR_FILE.exists():
        return {}
    try:
        data = json.loads(_CURSOR_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.warning("reasonix 导出游标损坏，按空处理: %s", _CURSOR_FILE)
        return {}


def _save_cursor(cursor: dict) -> None:
    """原子写游标（临时文件 + os.replace，防崩溃半写）。"""
    tmp = _CURSOR_FILE.with_name(_CURSOR_FILE.name + ".tmp")
    tmp.write_text(json.dumps(cursor, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _CURSOR_FILE)


def _next_started_at(last: str | None) -> str:
    """导出时刻 started_at：毫秒精度 + 单调递增兜底。

    与 adapters/hermes _append_delta 同款：引擎对同 session_key + 同
    started_at 幂等丢弃、不同则追加——毫秒精度保证相邻导出时间戳不同，
    单调兜底防时钟回拨/同毫秒连续导出碰撞（08-11 实测教训）。
    """
    from datetime import datetime, timedelta, timezone

    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if last and now_ts <= last:
        base = datetime.fromisoformat(last.replace("Z", "+00:00"))
        now_ts = (base + timedelta(milliseconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
    return now_ts


def fetch_sgme_has_session(session_key: str) -> bool:
    """查 SGME raw_files 是否已有该会话（/v1/admin/sessions 精确匹配）。

    本地游标缺失时的兜底种子：旧版（PR#1 全量导出）已入库的会话视为已导出，
    防升级后首次 SessionEnd 因 started_at 变为导出时刻而整段重复追加。
    失败/无记录 → False（不阻塞正常导出）。
    """
    cli = _http()
    if cli is None:
        return False
    try:
        r = cli.get(
            f"{_BASE_URL}/v1/admin/sessions",
            params={"session_key": session_key, "limit": 20},
            headers={"X-API-Key": _ADMIN_KEY},
        )
        if r.status_code != 200:
            return False
        data = r.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        return any(it.get("session_key") == session_key for it in items)
    except Exception as e:
        logger.warning("reasonix 查询 SGME 已有会话异常: %s", e)
        return False
    finally:
        cli.close()


# ---------- 注入（SessionStart，PR #2） ----------

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
        logger.warning("reasonix inject 异常: %s", e)
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
        logger.warning("reasonix search 异常: %s", e)
        return []
    finally:
        cli.close()


def build_start_context(cwd: str) -> str:
    """拼 SessionStart 注入上下文：身份说明 + 用户画像 + 项目相关记忆。

    与 adapters/hermes 的 system_prompt_block/prefetch 同格式，截断保护：
    additionalContext 单 hook 上限约 10000 字符（Reasonix 官方文档）。
    身份说明（首段）让模型**知道**自己在使用 SGME 记忆系统——机制就位不等于知情。
    """
    parts: list[str] = [
        "# SGME 记忆系统\n"
        "你的会话会被自动记录并提炼为长期记忆（跨 Agent 共享）。"
        "以下是与你相关的既有记忆，可直接引用；需要更多时用 `/sgme <关键词>` 查询。"
    ]

    blocks = fetch_inject()
    lines: list[str] = []
    for b in blocks:
        title = b.get("title", "")
        items = b.get("items", [])
        if not items:
            continue
        lines.append(f"【{title}】")
        for it in items:
            c = it.get("content", "")
            if c:
                lines.append(f"- {c}")
    if lines:
        lines.insert(0, "# 用户画像（SGME）")
        parts.append("\n".join(lines))

    # 项目相关记忆：以 cwd 目录名检索
    project = Path(cwd).name if cwd else ""
    if project:
        results = fetch_search(project)
        mem_lines = [f"- {it.get('content', '')}" for it in results if it.get("content")]
        if mem_lines:
            parts.append("# 相关记忆（SGME）\n" + "\n".join(mem_lines))

    text = "\n\n".join(parts).strip()
    return text[:9800]  # 单 hook 上限保护


def cmd_query(query: str) -> int:
    """/sgme 查询命令：检索 SGME 记忆（memory + wiki 两层）并输出。"""
    query = (query or "").strip()
    if not query:
        print("用法：/sgme <关键词>（检索 SGME 长期记忆）")
        return 0
    lines: list[str] = []
    # 记忆层（BM25+向量+RRF）
    cli = _http()
    if cli is None:
        print("SGME 不可达（bridge 依赖缺失）")
        return 0
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/search",
            json={"query": query[:200], "scopes": ["memory", "wiki"], "limit": 5},
            headers={"X-API-Key": _AGENT_KEY},
        )
        if r.status_code == 200:
            for it in r.json().get("results", []):
                src = it.get("source", "?")
                content = it.get("content", "")
                if content:
                    lines.append(f"[{src}] {content}")
    except Exception as e:
        logger.warning("reasonix query 异常: %s", e)
        lines.append(f"查询失败: {e}")
    finally:
        cli.close()

    if not lines:
        print(f"未找到与「{query}」相关的记忆")
        return 0
    print("\n".join(lines))
    return 0


def cmd_start(payload: dict) -> int:
    """SessionStart：注入 SGME 画像 + 相关记忆（stdout → 下一轮模型上下文）。"""
    cwd = payload.get("cwd", "")
    if not cwd:
        return 0
    context = build_start_context(cwd)
    if not context:
        return 0
    # Claude Code 兼容 JSON（Reasonix 文档：hookSpecificOutput.hookEventName 必须与事件一致）
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    return 0


# ---------- 入口 ----------

def cmd_end(payload: dict) -> int:
    """SessionEnd：增量导出 Reasonix 会话 → SGME L0 → 触发提炼。

    ST-23③（2026-08-11）：Reasonix 无每轮 hook 事件（v1.19.1 仅
    SessionStart/SessionEnd）→ 以 SessionEnd 为增量导出时机，持久化
    「已导出消息数」游标（.export_cursor.json）：重跑只 append 新增段，
    会话结束时的补齐自然覆盖最后一轮。每轮 append 用导出时刻作 started_at
    （毫秒精度 + 单调兜底，与 adapters/hermes 同款），恢复引擎追加语义。

    游标在 append 成功后才推进：失败重跑会重导同一段（本地服务响应丢失
    窗口极小，宁可重导不可丢消息）。本地游标缺失但 SGME 已有该会话时
    整段视为已导出（旧版全量导出的升级种子，防重复）。
    """
    session_id = payload.get("sessionId", "")
    cwd = payload.get("cwd", "")
    if not session_id:
        logger.warning("reasonix SessionEnd payload 缺 sessionId: %s", payload)
        return 0
    session_file = find_session_file(session_id, cwd)
    if session_file is None:
        logger.warning("reasonix 会话文件未找到: %s (cwd=%s)", session_id, cwd)
        return 0
    messages = parse_session_file(session_file)  # tool 消息已按指纹去重
    if not messages:
        logger.warning("reasonix 会话无有效消息: %s", session_id)
        return 0
    session_key = f"reasonix-{session_id.replace(' ', '_')}"

    cursor = _load_cursor()
    entry = cursor.get(session_key, {})
    exported = int(entry.get("exported", 0) or 0)
    if session_key not in cursor and fetch_sgme_has_session(session_key):
        # 升级种子：旧版全量导出已入库 → 整段视为已导出（防 started_at 变更致重复追加）
        exported = len(messages)
        entry = {"exported": exported}
    new_msgs = messages[exported:]
    if not new_msgs:
        logger.info("reasonix 会话无新增消息，跳过导出: %s", session_key)
        return 0

    last_started_at = entry.get("last_started_at")
    started_at = _next_started_at(last_started_at if isinstance(last_started_at, str) else None)
    l0_text = to_l0(new_msgs)
    if append_to_sgme(l0_text, session_key, started_at):
        # 导出成功后才推进游标（重跑幂等 + 崩溃不丢消息）
        cursor[session_key] = {"exported": len(messages), "last_started_at": started_at}
        _save_cursor(cursor)
        trigger_refine()
        logger.info("reasonix 会话增量入库: %s (+%d/%d 条)",
                    session_key, len(new_msgs), len(messages))
    else:
        logger.warning("reasonix 会话入库失败（游标未推进，重跑会重导本段）: %s",
                       session_key)
    return 0


def _read_payload() -> dict:
    """读 hook stdin payload（一行 JSON）。"""
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main(argv: list[str] | None = None, stdin: str | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    payload = _read_payload() if stdin is None else _json_or_empty(stdin)
    mode = argv[0] if argv else "--end"
    if mode == "--end":
        return cmd_end(payload)
    if mode == "--start":
        return cmd_start(payload)
    if mode == "--query":
        return cmd_query(argv[1] if len(argv) > 1 else "")
    logger.warning("未知模式: %s", mode)
    return 0


def _json_or_empty(text: str) -> dict:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}


if __name__ == "__main__":
    # Windows 控制台默认 GBK——hook stdout 必须 UTF-8（注入内容可能含 emoji）
    # errors='replace' 兜底：极端字符也绝不崩 hook
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
