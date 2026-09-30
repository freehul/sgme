from __future__ import annotations

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
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


class JsonCursorStore:
    """Atomic local cursor state; it contains no conversation content or credentials."""

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
    """Capture Codex turns into SGME L0 and close the session asynchronously."""

    def __init__(
        self, adapter: Any, *, session_key: str, cursor_store: JsonCursorStore
    ):
        self.adapter = adapter
        self.session_key = session_key
        self.cursor_store = cursor_store
        settings = getattr(adapter, "settings", None)
        self.agent_id = getattr(settings, "agent_id", "codex")

    def start(self, *, mode: str = "daily", max_tokens: int = 800) -> dict[str, Any]:
        return {
            "inject": self.adapter.inject(mode=mode, max_tokens=max_tokens),
            "signals": self.adapter.mcp_tool("signal_pull", limit=20),
        }

    def _next_started_at(self, requested: str | None) -> str:
        previous = self.cursor_store.get(self.session_key).get("last_started_at")
        candidate = _parse_timestamp(requested) if requested else datetime.now(UTC)
        if previous:
            candidate = max(
                candidate, _parse_timestamp(previous) + timedelta(milliseconds=1)
            )
        return _format_timestamp(candidate)

    @staticmethod
    def _conversation_blocks(
        messages: Iterable[Mapping[str, Any]], fallback_timestamp: str
    ) -> list[str]:
        blocks = []
        for message in messages:
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            timestamp = (
                message.get("timestamp") or message.get("ts") or fallback_timestamp
            )
            header = "#" if role == "user" else "##"
            blocks.append(f"{header} {timestamp} {role}\n{content}")
        return blocks

    def append_turn(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        started_at: str | None = None,
    ) -> Any:
        next_started_at = self._next_started_at(started_at)
        blocks = self._conversation_blocks(messages, next_started_at)
        if not blocks:
            raise ValueError("no user/assistant messages to append")
        content = "\n\n".join(blocks)
        result = self.adapter.append(
            session_key=self.session_key,
            started_at=next_started_at,
            agent_id=self.agent_id,
            content=content,
        )
        previous = self.cursor_store.get(self.session_key)
        self.cursor_store.update(
            self.session_key,
            {
                "last_started_at": next_started_at,
                "exported_messages": previous.get("exported_messages", 0) + len(blocks),
            },
        )
        return result

    def end(self) -> Any:
        return self.adapter.mcp_tool("refine_trigger", async_mode=True)
