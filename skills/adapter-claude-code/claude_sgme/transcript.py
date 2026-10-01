"""Claude Code 会话转录（transcript JSONL）解析。

只提取「真实对话」：user/assistant 的文本块。
过滤：旁支（isSidechain）、元消息（isMeta）、工具回执（toolUseResult /
tool_result 块）以及无文本的纯工具调用轮次。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            # 只看顶层 text 块；不下钻 tool_result 内部
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        return "\n\n".join(parts)
    return ""


def _is_conversation_record(record: dict[str, Any]) -> bool:
    if record.get("type") not in {"user", "assistant"}:
        return False
    if record.get("isSidechain"):
        return False
    if record.get("isMeta"):
        return False
    if record.get("toolUseResult") is not None:
        return False
    return isinstance(record.get("message"), dict)


def extract_messages(lines: Iterable[str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or not _is_conversation_record(record):
            continue
        message = record["message"]
        role = message.get("role") or record.get("type")
        if role not in {"user", "assistant"}:
            continue
        text = extract_text(message.get("content"))
        if not text.strip():
            continue
        messages.append(
            {
                "role": role,
                "content": text,
                "timestamp": record.get("timestamp") or "",
                "uuid": record.get("uuid") or "",
            }
        )
    return messages


def read_messages(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return extract_messages(handle)
