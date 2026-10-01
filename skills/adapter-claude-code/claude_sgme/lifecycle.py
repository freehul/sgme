"""会话生命周期：游标状态 + L0 追加桥。

游标只存「导出了多少条消息 + 最后一条的锚点」，不存对话内容与凭据。
增量策略：转录是只追加的 JSONL，按条数增量导出；若发现转录被重写
（锚点对不上 / 条数倒退）则全量重放——服务端对同内容幂等，安全。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _message_anchor(message: Mapping[str, Any]) -> str:
    uuid = str(message.get("uuid") or "")
    if uuid:
        return uuid
    content = str(message.get("content") or "")
    return "sha1:" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]


class JsonCursorStore:
    """原子写入的本地游标状态；不含对话内容与凭据。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"sessions": {}}
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
            raise TypeError(f"invalid SGME cursor file: {self.path}")
        return data

    def get(self, session_key: str) -> dict[str, Any]:
        return dict(self._load()["sessions"].get(session_key, {}))

    def update(self, session_key: str, state: Mapping[str, Any]) -> None:
        data = self._load()
        data["sessions"][session_key] = dict(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f"{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise


class SessionBridge:
    """把 Claude Code 的轮次导出到 SGME L0，并在会话收尾触发异步提炼。"""

    def __init__(self, adapter: Any, *, session_key: str, cursor_store: JsonCursorStore):
        self.adapter = adapter
        self.session_key = session_key
        self.cursor_store = cursor_store
        settings = getattr(adapter, "settings", None)
        self.agent_id = getattr(settings, "agent_id", "claude-code")

    def start(self, *, mode: str = "daily", max_tokens: int = 800) -> dict[str, Any]:
        """会话启动：画像注入 + 拉取未消费关怀信号。

        两路独立容错——任何一路失败不拖垮另一路，也不拖垮会话。
        """
        result: dict[str, Any] = {}
        try:
            result["inject"] = self.adapter.inject(mode=mode, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            result["inject_error"] = str(exc)[:200]
        try:
            result["signals"] = self.adapter.mcp_tool("signal_pull", limit=20)
        except Exception as exc:  # noqa: BLE001
            result["signals_error"] = str(exc)[:200]
        return result

    def _next_started_at(self, requested: str | None) -> str:
        previous = self.cursor_store.get(self.session_key).get("last_started_at")
        candidate = _parse_timestamp(requested) if requested else datetime.now(UTC)
        if previous:
            candidate = max(candidate, _parse_timestamp(str(previous)) + timedelta(milliseconds=1))
        return _format_timestamp(candidate)

    def append_turn(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        started_at: str | None = None,
    ) -> Any:
        msgs = [
            m
            for m in messages
            if isinstance(m, Mapping)
            and m.get("role") in {"user", "assistant"}
            and isinstance(m.get("content"), str)
            and m["content"].strip()
        ]
        state = self.cursor_store.get(self.session_key)
        exported = int(state.get("exported_messages") or 0)
        if exported > len(msgs) or (
            exported and state.get("last_anchor") != _message_anchor(msgs[exported - 1])
        ):
            # 转录被重写（压缩/恢复）→ 全量重放，服务端按内容幂等
            exported = 0
        pending = msgs[exported:]
        if not pending:
            return {"status": "noop", "exported": exported, "total": len(msgs)}

        next_started_at = self._next_started_at(started_at)
        blocks = []
        for message in pending:
            header = "#" if message["role"] == "user" else "##"
            timestamp = str(message.get("timestamp") or next_started_at)
            blocks.append(f"{header} {timestamp} {message['role']}\n{message['content']}")
        content = "\n\n".join(blocks)

        result = self.adapter.append(
            session_key=self.session_key,
            started_at=next_started_at,
            agent_id=self.agent_id,
            content=content,
        )
        self.cursor_store.update(
            self.session_key,
            {
                "last_started_at": next_started_at,
                "exported_messages": len(msgs),
                "last_anchor": _message_anchor(msgs[-1]),
            },
        )
        if isinstance(result, dict):
            result = {**result, "appended_messages": len(pending)}
        return result

    def end(self) -> Any:
        return self.adapter.mcp_tool("refine_trigger", async_mode=True)
