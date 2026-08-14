"""raw/store.py：L0 原始文件读写（frontmatter + 消息块 + 增量段）。

文件格式见 SGME-L0文件格式-v0.1.md：
- frontmatter：format_version/file_id/session_key/agent_id/source_type/started_at/metadata
- 消息块：`# {ISO} {role}`（首块）或 `## {ISO} {role}`（后续），role ∈ user/assistant/tool/system
- tool 块正文首行：`**tool**: {工具名}`
- 序号按解析顺序 1..n 生成（追加只增序号，既有序号不变）

本模块只管文件操作；raw_files 表更新由调用方（server 层）协调 session_dao。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from sgme import config

FORMAT_VERSION = 1

# 消息块标题正则：行首 # 或 ##，后跟 ISO 时间 + role
_MSG_HEADER_RE = re.compile(r"^#{1,2}\s+(\S+)\s+(user|assistant|tool|system)\s*$", re.MULTILINE)

# 合法 role
_VALID_ROLES = {"user", "assistant", "tool", "system"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 数据结构 ----------

@dataclass
class Message:
    """单条消息（解析后）。"""
    seq: int                # 1-based 序号
    timestamp: str          # ISO 8601
    role: str               # user/assistant/tool/system
    content: str            # 正文（不含标题行）
    tool_name: str | None = None  # 仅 tool 块：工具名


@dataclass
class ParsedFile:
    """解析后的 L0 文件。"""
    frontmatter: dict
    messages: list[Message] = field(default_factory=list)
    path: str | None = None


class L0FormatError(Exception):
    """L0 文件格式错误（frontmatter 缺失/格式版本不识别/消息块解析失败）。"""


# ---------- 文件路径 ----------

def _file_path(file_id: str, source_type: str = "session") -> Path:
    """根据 source_type 返回 raw/ 下的子目录路径（不含 .md 后缀的 Path）。"""
    sub = {"session": "sessions", "upload": "uploads", "external": "external"}.get(
        source_type, "sessions"
    )
    return config.RAW_DIR / sub / f"{file_id}.md"


def file_path(file_id: str, source_type: str = "session") -> Path:
    """公开：返回 L0 文件绝对路径。"""
    return _file_path(file_id, source_type)


# ---------- 写新文件 ----------

def _format_frontmatter(meta: dict) -> str:
    """生成 frontmatter 文本（--- 包裹的 YAML）。"""
    fm = {
        "format_version": FORMAT_VERSION,
        "file_id": meta["file_id"],
        "session_key": meta["session_key"],
        "agent_id": meta.get("agent_id"),
        "source_type": meta.get("source_type", "session"),
        "started_at": meta["started_at"],
    }
    if meta.get("metadata"):
        fm["metadata"] = meta["metadata"]
    # yaml.safe_dump 保留中文（allow_unicode=True），禁用流式风格
    body = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{body}---\n"


def _format_message_block(msg: dict, is_first: bool) -> str:
    """格式化单条消息块文本。

    msg: {timestamp, role, content, tool_name?}
    """
    heading = "#" if is_first else "##"
    role = msg["role"]
    ts = msg["timestamp"]
    content = msg.get("content", "")
    parts = [f"{heading} {ts} {role}\n"]
    if role == "tool" and msg.get("tool_name"):
        parts.append(f"**tool**: {msg['tool_name']}\n\n")
    parts.append(f"{content}\n" if content and not content.endswith("\n") else content)
    # 块间空行分隔
    if not parts[-1].endswith("\n\n"):
        parts.append("\n")
    return "".join(parts)


def write_new_file(
    file_id: str,
    session_key: str,
    started_at: str | None = None,
    agent_id: str | None = None,
    source_type: str = "session",
    first_messages: list[dict] | None = None,
    metadata: dict | None = None,
) -> Path:
    """写新 L0 文件（frontmatter + 首批消息块）。

    - file_id: UUID4
    - first_messages: [{timestamp, role, content, tool_name?}, ...]
    - 返回文件路径
    """
    path = _file_path(file_id, source_type)
    path.parent.mkdir(parents=True, exist_ok=True)

    meta = {
        "file_id": file_id,
        "session_key": session_key,
        "agent_id": agent_id,
        "source_type": source_type,
        "started_at": started_at or _now_iso(),
        "metadata": metadata,
    }
    parts = [_format_frontmatter(meta)]
    msgs = first_messages or []
    for i, m in enumerate(msgs):
        parts.append(_format_message_block(m, is_first=(i == 0)))
    path.write_text("".join(parts), encoding="utf-8")
    return path


# ---------- 追加 ----------

def append_messages(
    file_id: str,
    messages: list[dict],
    source_type: str = "session",
) -> Path:
    """文件尾部原子追加消息块（用 ## 标题）。

    - 已有内容与序号不变
    - 追加方无需解析全文，只需文件存在则用 ##
    """
    path = _file_path(file_id, source_type)
    if not path.exists():
        raise FileNotFoundError(f"L0 文件不存在: {path}")
    # 原子追加：用 'a' 模式
    with path.open("a", encoding="utf-8") as f:
        for m in messages:
            f.write(_format_message_block(m, is_first=False))
    return path


# ---------- 解析 ----------

def parse_file(file_id: str, source_type: str = "session") -> ParsedFile:
    """解析 L0 文件：frontmatter + 消息块切分 + seq 编号。

    frontmatter 缺失/格式版本不识别 → 抛 L0FormatError。
    """
    path = _file_path(file_id, source_type)
    if not path.exists():
        raise FileNotFoundError(f"L0 文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    return parse_text(text, path=str(path))


def parse_text(text: str, path: str | None = None) -> ParsedFile:
    """解析 L0 文本（供测试与 parse_file 共用）。"""
    # frontmatter：首行必须是 ---，到下一个 --- 结束
    if not text.startswith("---"):
        raise L0FormatError("frontmatter 缺失（文件不以 --- 开头）")
    # 找闭合 ---
    # 跳过首行 ---，找下一个独立行 ---
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise L0FormatError("frontmatter 起始 --- 异常")
    fm_end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end_idx = i
            break
    if fm_end_idx is None:
        raise L0FormatError("frontmatter 未闭合（缺 --- 结束）")

    fm_text = "".join(lines[1:fm_end_idx])
    try:
        frontmatter = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise L0FormatError(f"frontmatter YAML 解析失败: {e}") from e

    if not isinstance(frontmatter, dict):
        raise L0FormatError("frontmatter 不是字典")
    if "file_id" not in frontmatter or "session_key" not in frontmatter:
        raise L0FormatError("frontmatter 缺必要字段 file_id/session_key")
    fv = frontmatter.get("format_version")
    if fv is None or int(fv) != FORMAT_VERSION:
        raise L0FormatError(f"format_version 不识别: {fv}（当前支持 {FORMAT_VERSION}）")

    body = "".join(lines[fm_end_idx + 1:])

    # 切消息块：按 ^#{1,2} {ISO} {role} 切分
    matches = list(_MSG_HEADER_RE.finditer(body))
    messages: list[Message] = []
    for i, m in enumerate(matches):
        ts = m.group(1)
        role = m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip("\n")
        # tool 块解析首行工具名
        tool_name = None
        if role == "tool":
            # 首行 **tool**: {name}
            tool_match = re.match(r"^\*\*tool\*\*:\s*(\S+)", content)
            if tool_match:
                tool_name = tool_match.group(1)
                # 去掉首行，保留工具输出
                content = content.split("\n", 1)[1].strip("\n") if "\n" in content else ""
        messages.append(Message(
            seq=i + 1, timestamp=ts, role=role,
            content=content, tool_name=tool_name,
        ))

    return ParsedFile(frontmatter=frontmatter, messages=messages, path=path)


# ---------- 增量段 ----------

def extract_incremental(parsed: ParsedFile, last_refined_seq: int | None) -> list[Message]:
    """提取增量段：seq > last_refined_seq 的消息块。

    - last_refined_seq=None 或 0 → 全量
    """
    if not last_refined_seq:
        return list(parsed.messages)
    return [m for m in parsed.messages if m.seq > last_refined_seq]


# ---------- 纯消息体解析（无 frontmatter，供 /v1/append 用） ----------

def parse_body_messages(text: str) -> list[Message]:
    """解析纯消息块文本（无 frontmatter）→ Message 列表。

    供 server /v1/append 端点用：API content 字段即消息块文本。
    复用 _MSG_HEADER_RE 切分；seq 从 1 起编号。
    """
    body = text if text.startswith("#") else text.lstrip()
    matches = list(_MSG_HEADER_RE.finditer(body))
    messages: list[Message] = []
    for i, m in enumerate(matches):
        ts = m.group(1)
        role = m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip("\n")
        tool_name = None
        if role == "tool":
            tool_match = re.match(r"^\*\*tool\*\*:\s*(\S+)", content)
            if tool_match:
                tool_name = tool_match.group(1)
                content = content.split("\n", 1)[1].strip("\n") if "\n" in content else ""
        messages.append(Message(
            seq=i + 1, timestamp=ts, role=role,
            content=content, tool_name=tool_name,
        ))
    return messages


# ---------- 校验 / 健康检查 ----------

def validate_or_mark_error(
    file_id: str,
    source_type: str = "session",
) -> tuple[bool, str | None]:
    """校验 L0 文件格式；失败返回 (False, reason)。

    供 server 层在解析失败时标记 raw_files.status=error + anomaly_warn。
    """
    try:
        parse_file(file_id, source_type)
        return True, None
    except L0FormatError as e:
        return False, str(e)
    except FileNotFoundError as e:
        return False, str(e)


def file_size(file_id: str, source_type: str = "session") -> int:
    """返回文件字节数。"""
    path = _file_path(file_id, source_type)
    return path.stat().st_size if path.exists() else 0


def relative_path(file_id: str, source_type: str = "session") -> str:
    """返回相对用户根的路径（存入 raw_files.path，T-23 跟随 SGME_HOME）。"""
    p = _file_path(file_id, source_type)
    try:
        return str(p.relative_to(config.USER_ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)
