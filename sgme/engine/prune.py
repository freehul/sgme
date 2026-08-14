"""engine/prune.py：L0 消息剪枝（先剪枝后分块，2026-08-06 新增）。

背景：Hermes 原始会话中 tool 输出平均占 95% 字符（实测 236K → 11K），
这些 JSON 工具输出对记忆提炼几乎无价值，却撑爆分块数量、稀释信噪比，
导致 9B 模型长输入角色崩坏（8K 甜点区外复读原文而非输出 JSON）。

策略（按消息角色）：
- tool：默认只保留工具名（drop 输出）；可配置 truncate 保留前 N 字符
- user/assistant：跳过系统注入前缀消息（[PRIOR CONTEXT]/[ASYNC DELEGATION]/MEDIA:）
- 单条超长消息：截断到 max_msg_chars（防单消息撑爆甜点区）

配置（config/sgme.yaml → prune 段）：
  prune:
    tool_output: drop | truncate | keep    # 默认 drop
    tool_truncate_chars: 300                # truncate 时保留的输出长度
    skip_system_prefixes: ["[PRIOR CONTEXT]", "[ASYNC DELEGATION]", "MEDIA:"]
    max_msg_chars: 4000                     # 单条消息截断上限（0=不截断）
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("sgme.engine.prune")

DEFAULT_CONFIG = {
    "tool_output": "drop",           # drop | truncate | keep
    "tool_truncate_chars": 300,
    "skip_system_prefixes": [
        "[PRIOR CONTEXT]", "[ASYNC DELEGATION]", "MEDIA:",
    ],
    "max_msg_chars": 4000,
}

# 已知系统注入前缀（Hermes 平台消息）
_SYSTEM_PREFIXES = (
    "[PRIOR CONTEXT]",
    "[ASYNC DELEGATION]",
    "MEDIA:",
    "[SYSTEM]",
)


def merge_prune_config(cfg: dict | None) -> dict:
    """合并用户配置与默认值。"""
    merged = dict(DEFAULT_CONFIG)
    if cfg:
        for k, v in cfg.items():
            if k in merged and v is not None:
                merged[k] = v
    return merged


def _is_system_injection(content: str, prefixes: list[str]) -> bool:
    """判断消息是否为系统注入（前缀匹配）。"""
    stripped = content.lstrip()
    for p in prefixes:
        if stripped.startswith(p):
            return True
    return False


def _truncate(content: str, limit: int) -> str:
    """按字符截断，保留开头（工具输出头部通常含摘要/状态）。"""
    if limit <= 0 or len(content) <= limit:
        return content
    return content[:limit] + f"\n...[截断 {len(content) - limit} 字符]"


def prune_messages(
    messages: list[Any],
    cfg: dict | None = None,
) -> list[Any]:
    """剪枝消息列表，返回干净列表（原地过滤 + 内容截断）。

    - 保留原 Message 对象结构（seq/timestamp/role/tool_name/content）
    - tool 输出按配置 drop/truncate/keep
    - 系统注入消息直接丢弃
    - 超长单条消息截断
    """
    pc = merge_prune_config(cfg)
    tool_mode = pc["tool_output"]
    skip_prefixes = pc["skip_system_prefixes"]
    max_chars = pc["max_msg_chars"]

    kept: list[Any] = []
    dropped_tool = 0
    dropped_system = 0

    for m in messages:
        role = getattr(m, "role", "")
        content = getattr(m, "content", "") or ""

        # 1. 系统注入过滤（user/assistant 消息）
        if role in ("user", "assistant") and _is_system_injection(content, skip_prefixes):
            dropped_system += 1
            continue

        # 2. tool 输出处理
        if role == "tool":
            if tool_mode == "drop":
                # 无 tool_name 时直接丢弃（无信息量空壳）；
                # 有 tool_name 时保留工具名（知道执行了什么），输出丢弃
                if not getattr(m, "tool_name", None):
                    dropped_tool += 1
                    continue
                dropped_tool += 1
                if hasattr(m, "content"):
                    m.content = ""
            elif tool_mode == "truncate":
                m.content = _truncate(content, pc["tool_truncate_chars"])
            # keep: 原样保留

        # 3. 单条超长截断（非 tool，或 keep 模式下的 tool）
        elif max_chars > 0 and len(content) > max_chars:
            m.content = _truncate(content, max_chars)

        kept.append(m)

    if dropped_tool or dropped_system:
        logger.info(
            "剪枝: 丢弃 tool 输出 %d 条, 系统注入 %d 条, 保留 %d 条",
            dropped_tool, dropped_system, len(kept),
        )
    return kept


def prune_stats(messages_before: int, messages_after: list[Any]) -> dict:
    """剪枝统计（用于日志/审计）。"""
    total_chars = sum(len(getattr(m, "content", "") or "") for m in messages_after)
    return {
        "messages_before": messages_before,
        "messages_after": len(messages_after),
        "chars_after": total_chars,
    }
