"""Claude Code hooks 处理器。

三个事件：
- SessionStart → 注入画像 + 拉取未消费关怀信号（作为 additionalContext 输出）
- Stop         → 把新产生的对话轮次增量追加到 SGME L0（游标对账，服务端幂等）
- SessionEnd   → 收尾补一次追加 + 触发异步提炼

铁律：钩子永不阻塞会话——任何异常都吞掉并写日志，退出码恒为 0。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapter import SgmeAdapter
from .lifecycle import JsonCursorStore, SessionBridge
from .transcript import read_messages

STATE_DIR = Path.home() / ".claude-sgme"
CURSOR_FILE = STATE_DIR / "state" / "cursor.json"
LOG_FILE = STATE_DIR / "logs" / "hook.log"
LOG_MAX_BYTES = 512 * 1024


def _log(line: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.write_text("", encoding="utf-8")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {line}\n")
    except OSError:
        pass


def _configure_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        rc = getattr(stream, "reconfigure", None)
        if callable(rc):
            try:
                rc(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def _tool_text(result: Any) -> str:
    """从 MCP 工具结果（content 块数组）拼接文本。"""
    if not isinstance(result, dict):
        return ""
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n".join(parts)


def _compose_context(data: dict[str, Any]) -> str:
    lines = ["【SGME 拾光记忆引擎】已连接（agent_id=claude-code）。"]
    injected = _tool_text(data.get("inject"))
    if injected.strip():
        lines.append("— 画像注入 —\n" + injected.strip()[:1600])
    if data.get("inject_error"):
        lines.append(f"（画像注入暂不可用：{data['inject_error']}）")
    signals = _tool_text(data.get("signals")).strip()
    if signals and signals not in {"[]", "{}"} and '"count": 0' not in signals and '"events": []' not in signals:
        lines.append("— 未消费关怀信号（claim → 关怀 → ack）—\n" + signals[:1200])
    return "\n\n".join(lines)


def _session_key(payload: dict[str, Any]) -> str:
    return f"claude-{payload.get('session_id') or 'unknown'}"


def run_session_start(payload: dict[str, Any], adapter: SgmeAdapter) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "unknown")
    bridge = SessionBridge(
        adapter, session_key=_session_key(payload), cursor_store=JsonCursorStore(CURSOR_FILE)
    )
    data = bridge.start()
    _log(f"session-start: session={session_id[:8]} keys={sorted(k for k in data)}")
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _compose_context(data),
        }
    }


def run_stop(payload: dict[str, Any], adapter: SgmeAdapter) -> None:
    session_id = str(payload.get("session_id") or "unknown")
    transcript = payload.get("transcript_path")
    if not transcript or not Path(str(transcript)).exists():
        _log(f"stop: no transcript for session={session_id[:8]}")
        return
    messages = read_messages(str(transcript))
    bridge = SessionBridge(
        adapter, session_key=_session_key(payload), cursor_store=JsonCursorStore(CURSOR_FILE)
    )
    result = bridge.append_turn(messages)
    _log(
        f"stop: session={session_id[:8]} total={len(messages)} "
        f"result={json.dumps(result, ensure_ascii=False, default=str)[:200]}"
    )


def run_session_end(payload: dict[str, Any], adapter: SgmeAdapter) -> None:
    session_id = str(payload.get("session_id") or "unknown")
    try:
        run_stop(payload, adapter)
    except Exception as exc:  # noqa: BLE001
        _log(f"session-end: final append failed: {exc}")
    try:
        bridge = SessionBridge(
            adapter, session_key=_session_key(payload), cursor_store=JsonCursorStore(CURSOR_FILE)
        )
        result = bridge.end()
        _log(
            f"session-end: session={session_id[:8]} reason={payload.get('reason')} "
            f"refine={json.dumps(result, ensure_ascii=False, default=str)[:160]}"
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"session-end: refine trigger failed: {exc}")


EVENTS: dict[str, Any] = {
    "session-start": run_session_start,
    "stop": run_stop,
    "session-end": run_session_end,
}


def run(event: str) -> int:
    """hooks 入口：stdin 读 Claude Code 的 hook JSON → 分发 → 输出。退出码恒为 0。"""
    _configure_utf8()
    payload: dict[str, Any] = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                payload = decoded
    except Exception as exc:  # noqa: BLE001
        _log(f"hook {event}: stdin parse failed: {exc}")

    handler = EVENTS.get(event)
    if handler is None:
        sys.stderr.write(f"claude-sgme: unknown hook event {event!r}\n")
        return 0

    adapter = None
    try:
        adapter = SgmeAdapter.from_env()
        output = handler(payload, adapter)
        if output:
            sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001
        _log(f"hook {event}: ERROR {type(exc).__name__}: {exc}")
        sys.stderr.write(f"claude-sgme hook {event}: {exc}\n")
        return 0
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
