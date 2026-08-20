"""engine/l1.py：L1 提取（prompt 渲染 + LLM 调用 + 输出校验）。

- render_l1: 经 PromptStore 读 prompts/l1_extraction.txt（支持 A/B 与钉版），替换 {{conversation}} 与 {{dimensions}}
- parse_l1_output: 严格 JSON 数组解析 + 字段校验（失败重试 1 次 → RefineError）
- extract_l1: 完整 L1 提取（render → call_with_fallback → parse），返回版本元信息 + 逐块记录 refine_run
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from sgme import config
from sgme.llm import chain as llm_chain
from sgme.llm import provider as llm_provider
from sgme.prompts import BucketCtx, PromptStore

logger = logging.getLogger("sgme.engine.l1")

# 记忆类型白名单
VALID_MEMORY_TYPES = {"persona", "episodic", "instruction"}
VALID_TIME_VELOCITY = {"static", "dynamic"}


class RefineError(Exception):
    """L1 提炼失败（JSON 解析/校验失败）。"""


# ---------- prompt 渲染 ----------

def render_l1(conversation: str, dimensions: list[dict], ctx: BucketCtx | None = None) -> str:
    """渲染 L1 提取 prompt（模板经 PromptStore 读取，支持 A/B 与钉版）。

    - {{dimensions}} = 注册表 active 维度的 "id：display_name" 列表（动态生成）
    - {{conversation}} = 会话文本
    """
    template = PromptStore().get("l1_extraction", ctx).text
    return _render_l1_text(template, conversation, dimensions)


def _render_l1_text(template: str, conversation: str, dimensions: list[dict]) -> str:
    """渲染已读出的模板文本（{{dimensions}} + {{conversation}}）。

    T-11：维度行附 boundaries（vs 对照消歧说明）——此前 import 静默丢弃、
    提示词只拿到 id：display_name，维度混淆风险缓解手段未生效（审计 D8）。
    """
    dim_lines = []
    for d in dimensions:
        if d.get("active", 1) == 1:
            line = f"- {d['id']}：{d['display_name']}"
            b = d.get("boundaries")
            if b:
                line += f"（边界：{b}）"
            dim_lines.append(line)
    dim_text = "\n".join(dim_lines) if dim_lines else "- (无可用维度)"
    return template.replace("{{dimensions}}", dim_text).replace("{{conversation}}", conversation)


# ---------- JSON 解析与校验 ----------

def _extract_json_array(text: str) -> list[dict]:
    """从 LLM 输出中提取 JSON 数组（容忍前后多余文字 + ```json 代码块）。"""
    text = text.strip()
    # 去除 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # 直接解析整个文本
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试找第一个 [ 到最后一个 ]
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError(f"期望 JSON 数组，得到 {type(data).__name__}")
    return data


def _validate_item(item: Any, dimensions: list[dict]) -> dict | None:
    """校验单条记忆，返回规整后的 dict 或 None（无效则跳过）。

    校验规则（spec.md T4）：
    - dimensions 非空
    - priority 0-100 钳制
    - time_velocity ∈ {static, dynamic}（否则按维度默认回填）
    - memory_type ∈ {persona, episodic, instruction}
    - content 非空
    """
    if not isinstance(item, dict):
        return None
    content = item.get("content")
    if not content or not isinstance(content, str):
        return None
    dims = item.get("dimensions")
    if not isinstance(dims, list) or not dims:
        return None
    memory_type = item.get("memory_type")
    if memory_type not in VALID_MEMORY_TYPES:
        # 不合法类型 → 默认 persona
        memory_type = "persona"
    priority = item.get("priority", 50)
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 50
    # 钳制 0-100
    priority = max(0, min(100, priority))
    time_velocity = item.get("time_velocity")
    if time_velocity not in VALID_TIME_VELOCITY:
        # 按维度默认回填：任一维度 dynamic → dynamic，否则 static
        dim_ids = {d["id"] for d in dimensions}
        dyn = any(d.get("time_velocity") == "dynamic" for d in dimensions
                  if d["id"] in dims)
        time_velocity = "dynamic" if dyn else "static"
    source_ids = item.get("source_message_ids", [])
    if not isinstance(source_ids, list):
        source_ids = []
    # ST-18 替代联动：透传 supersedes（str → [str]；list → 过滤非法项；缺省 → []）
    supersedes = item.get("supersedes")
    if isinstance(supersedes, str):
        supersedes = [supersedes] if supersedes.strip() else []
    elif isinstance(supersedes, list):
        supersedes = [s for s in supersedes if isinstance(s, str) and s.strip()]
    else:
        supersedes = []
    return {
        "content": content,
        "dimensions": [str(x) for x in dims],  # 归一化前的原始标签
        "memory_type": memory_type,
        "priority": priority,
        "time_velocity": time_velocity,
        "source_message_ids": source_ids,
        "supersedes": supersedes,
    }


def parse_l1_output(text: str, dimensions: list[dict]) -> list[dict]:
    """解析 L1 输出为记忆列表。

    严格 JSON 数组 + 字段校验。失败抛 RefineError。
    """
    try:
        data = _extract_json_array(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise RefineError(f"JSON 解析失败: {e}") from e
    result = []
    for item in data:
        v = _validate_item(item, dimensions)
        if v is not None:
            result.append(v)
    return result


# ---------- 完整 L1 提取 ----------

# 消息块正则：`# <ts> <role>` 行开头（L0 文件格式）
_MSG_RE = re.compile(r"^# \S+ (user|assistant|tool)\n", re.MULTILINE)


def chunk_messages_by_turn(
    messages: list,
    chunk_size: int = 5000,
    min_chunk: int | None = None,
) -> list[list]:
    """按回合语义分块（2026-08-06 新增，替代纯长度切块）。

    问题：固定字符切块即使按消息边界切，也会把「user 问题 + assistant 回答」
    拆到不同块，模型看到半截上下文，提取的记忆无效。

    方案（区间自适应，甜点区精测 4500~5500 / 峰值 5000）：
    - 回合 = 一条 user 消息 + 其后所有 assistant/tool 消息（直到下一条 user）
    - 以回合为最小单元，**区间自适应填充**：累积到下界 min_chunk 后，
      若下一个回合放不下（会超上界 chunk_size）才落块
    - 效果：块大小落在 [min_chunk, chunk_size] 区间，既不过碎也不吞尾巴
    - 单回合超上界时独立成块（不截断，宁大勿碎）
    - 无 user 消息的会话（系统生成）按条数 20 条/块兜底
    - **计量口径 = 格式化后字符**（含 [msg#] 头/前缀/换行，与 refine._format_conversation
      及甜点区测试一致；纯 content 计量会低估 ~30-40 字符/条，导致实际块偏大）

    返回消息块列表（每块内部语义完整）。
    """
    if not messages:
        return []

    if min_chunk is None:
        min_chunk = max(1000, int(chunk_size * 0.9))  # 默认下界 = 上界 90%

    def _size(msgs: list) -> int:
        """格式化后字符数（与 _format_conversation 输出一致）。"""
        total = 0
        for m in msgs:
            header = f"[msg#{getattr(m, 'seq', '')}] {getattr(m, 'timestamp', '')} {getattr(m, 'role', '')}:"
            total += len(header) + 4 + len(getattr(m, "content", "") or "")  # 2空格前缀 + 2换行
        return total

    # 1. 组装回合
    turns: list[list] = []
    current: list = []
    for m in messages:
        role = getattr(m, "role", "")
        if role == "user" and current:
            turns.append(current)
            current = []
        current.append(m)
    if current:
        turns.append(current)

    # 纯 assistant/tool 会话（无 user 头）：按条数兜底
    has_user = any(getattr(m, "role", "") == "user" for m in messages)
    if not has_user:
        return [messages[i:i + 20] for i in range(0, len(messages), 20)]

    # 2. 区间自适应填充（不拆回合）
    chunks: list[list] = []
    cur_chunk: list = []
    cur_size = 0
    for turn in turns:
        turn_size = _size(turn)
        # 已到下界 且 当前块加不下这个回合（会超上界）→ 落块
        if cur_chunk and cur_size >= min_chunk and cur_size + turn_size > chunk_size:
            chunks.append(cur_chunk)
            cur_chunk = []
            cur_size = 0
        cur_chunk.extend(turn)
        cur_size += turn_size
    if cur_chunk:
        chunks.append(cur_chunk)

    # 3. 超限单回合独立成块（宁大勿碎）
    final: list[list] = []
    for c in chunks:
        size = _size(c)
        if size > chunk_size * 1.5:
            # 超 1.5 倍：按消息边界切（带重叠），保底
            final.extend(_split_oversized(c, chunk_size))
        else:
            final.append(c)
    return final


def _split_oversized(messages: list, chunk_size: int) -> list[list]:
    """超长块保底拆分：按消息边界切，块间重叠 1 条消息。"""
    chunks: list[list] = []
    cur: list = []
    cur_size = 0
    for m in messages:
        size = len(getattr(m, "content", "") or "")
        if cur and cur_size + size > chunk_size:
            chunks.append(cur)
            # 重叠：保留当前块最后 1 条消息
            cur = [cur[-1]] if cur else []
            cur_size = sum(len(getattr(x, "content", "") or "") for x in cur)
        cur.append(m)
        cur_size += size
    if cur:
        chunks.append(cur)
    return chunks


def chunk_conversation(
    conversation: str,
    chunk_size: int = 8000,
    overlap: int = 1500,
) -> list[str]:
    """按消息边界分块（L1 输入长度甜点区 6-8K 字符，2026-08-04 实测）。

    - 块间重叠 overlap 字符（防切碎话题）
    - 严格在消息边界切分（# ts role 行），不在消息中间断开
    - 单条消息超过 chunk_size 时单独成块（不截断）
    - 返回块列表（按顺序）
    """
    if len(conversation) <= chunk_size:
        return [conversation]

    # 切出消息起点（行号）
    starts = [m.start() for m in _MSG_RE.finditer(conversation)]
    if not starts:
        # 无标准消息结构 → 直接按字符硬切（兜底）
        return [
            conversation[i:i + chunk_size]
            for i in range(0, len(conversation), chunk_size)
        ]

    chunks: list[str] = []
    seg_start = 0
    while seg_start < len(conversation):
        limit = seg_start + chunk_size
        if limit >= len(conversation):
            chunks.append(conversation[seg_start:])
            break
        # 找 limit 前最后一个消息起点（tail）：用 bisect 思路线性扫
        tail = None
        for s in starts:
            if seg_start < s <= limit:
                tail = s
            elif s > limit:
                break
        if tail is None:
            # 段内无新消息起点 → 若 seg_start 本身是消息头（超长消息），完整保留到下一条消息
            if seg_start in starts:
                nxt = next((s for s in starts if s > seg_start), len(conversation))
                chunks.append(conversation[seg_start:nxt])
                seg_start = nxt
            else:
                # 非消息头起点（理论上不达）→ 硬切到 limit，保证前进
                chunks.append(conversation[seg_start:limit])
                seg_start = limit
            continue
        chunks.append(conversation[seg_start:tail])
        # 下一块起点：tail 回退 overlap（取 <= tail-overlap 的最近消息起点）
        overlap_target = tail - overlap
        next_start = tail
        for s in starts:
            if s < seg_start:
                continue
            if s <= overlap_target:
                next_start = s
            else:
                break
        seg_start = next_start
        # 防死循环：必须严格前进（next_start 至少 > seg_start 旧值）
        if seg_start >= tail:
            seg_start = tail
    return chunks


def extract_l1(
    conversation: str | list[str],
    dimensions: list[dict],
    llm_cfg: dict,
    client=None,
    chunk_size: int | None = None,
    overlap: int | None = None,
    bucket_ctx: BucketCtx | None = None,
    mem_conn: sqlite3.Connection | None = None,
) -> tuple[list[dict], str, dict]:
    """完整 L1 提取：render → call_with_fallback → parse。

    - conversation 可传字符串（内部按消息边界分块）或**预分块列表**
      （2026-08-06：refine.py 已按回合语义预分块，直接逐块提炼，不再二次切分）
    - 长会话按消息边界分块（甜点区 8K），每块独立提炼后合并去重
    - 单块 JSON 坏输出重试 1 次（重问 LLM）
    - 再失败 → RefineError
    - bucket_ctx：A/B 分流上下文（提炼链路传 file_id）；mem_conn 提供时逐块记录 refine_run
    - 返回 (记忆列表, provider_name, prompt_meta)
      prompt_meta = {"stage": "l1_extraction", "version": ..., "variant": ...}；
      分块混合版本时 version="chunked" 并附 chunks 明细
    """
    if chunk_size is None:
        chunk_size = 8000
    if overlap is None:
        overlap = 1500

    if isinstance(conversation, list):
        # 预分块模式：调用方已按语义回合分块，直接逐块提炼
        chunks = conversation
        total_chars = sum(len(c) for c in chunks)
    else:
        chunks = chunk_conversation(conversation, chunk_size, overlap)
        total_chars = len(conversation)
    if len(chunks) == 1:
        return _extract_l1_chunk(
            chunks[0], dimensions, llm_cfg, client=client,
            bucket_ctx=bucket_ctx, mem_conn=mem_conn,
        )

    # 多块：逐块提炼 + 按 content 去重合并
    logger.info("L1 长会话分块提炼: %d 字符 → %d 块", total_chars, len(chunks))
    all_memories: list[dict] = []
    seen_contents: set[str] = set()
    chunk_metas: list[dict] = []
    for idx, chunk in enumerate(chunks):
        try:
            memories, _, meta = _extract_l1_chunk(
                chunk, dimensions, llm_cfg, client=client,
                bucket_ctx=bucket_ctx, mem_conn=mem_conn,
            )
            chunk_metas.append(meta)
        except RefineError as e:
            logger.warning("L1 块 %d/%d 失败: %s", idx + 1, len(chunks), e)
            continue
        for m in memories:
            key = m.get("content", "").strip()
            if key and key not in seen_contents:
                seen_contents.add(key)
                all_memories.append(m)
    logger.info("L1 分块合并: 原始 %d 块 → 去重后 %d 条记忆", len(chunks), len(all_memories))
    if not all_memories and chunks:
        raise RefineError(f"L1 全部分块失败（{len(chunks)} 块）")
    # 合并版本元信息：全部块版本一致 → 用该版本；混合 → "chunked"（精确信息在 refine_runs）
    if chunk_metas:
        versions = {m["version"] for m in chunk_metas}
        if len(versions) == 1:
            meta = {"stage": "l1_extraction", "version": versions.pop(), "variant": chunk_metas[0]["variant"]}
        else:
            meta = {"stage": "l1_extraction", "version": "chunked", "variant": None, "chunks": chunk_metas}
    else:
        meta = {"stage": "l1_extraction", "version": "chunked", "variant": None, "chunks": []}
    return all_memories, "chunked", meta


def _extract_l1_chunk(
    conversation: str,
    dimensions: list[dict],
    llm_cfg: dict,
    client=None,
    bucket_ctx: BucketCtx | None = None,
    mem_conn: sqlite3.Connection | None = None,
) -> tuple[list[dict], str, dict]:
    """单块 L1 提取（PromptStore 版本 + refine_run 逐块记录）。"""
    from sgme.data.refine_dao import RefineRunRecorder

    pv = PromptStore().get("l1_extraction", bucket_ctx)
    prompt = _render_l1_text(pv.text, conversation, dimensions)
    bucket_key = bucket_ctx.bucket_key if (bucket_ctx and bucket_ctx.bucket_key) else "unknown"
    max_attempts = 2  # 首次 + 重试 1 次
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            text, provider_name, usage = llm_chain.call_with_fallback(
                llm_cfg, prompt, chain_name="refinement", client=client,
            )
        except llm_provider.LLMUnavailable as e:
            if mem_conn is not None:
                err_run = RefineRunRecorder.start(
                    mem_conn, file_id=bucket_key, stage="l1_extraction",
                    version=pv.version, variant=pv.variant,
                    provider="unavailable", bucket_key=bucket_key,
                )
                RefineRunRecorder.finish(
                    mem_conn, err_run, memories_count=0, action_counts={},
                    status="error", error=str(e),
                )
            raise RefineError(f"LLM 全链失败: {e}") from e

        try:
            memories = parse_l1_output(text, dimensions)
            if attempt > 1:
                logger.info("L1 重试 %s 次成功", attempt)
            if mem_conn is not None:
                run_id = RefineRunRecorder.start(
                    mem_conn, file_id=bucket_key, stage="l1_extraction",
                    version=pv.version, variant=pv.variant,
                    provider=provider_name, bucket_key=bucket_key,
                )
                RefineRunRecorder.finish(
                    mem_conn, run_id, memories_count=len(memories),
                    action_counts={}, status="ok", usage=usage,
                )
            meta = {"stage": "l1_extraction", "version": pv.version, "variant": pv.variant}
            logger.info("L1 块完成: version=%s variant=%s memories=%d",
                        pv.version, pv.variant, len(memories))
            return memories, provider_name, meta
        except RefineError as e:
            last_error = e
            logger.warning("L1 输出解析失败 (attempt=%s): %s", attempt, e)
            # 重试时 prompt 加提示
            if attempt < max_attempts:
                prompt = _render_l1_text(pv.text, conversation, dimensions) + \
                    "\n\n# 注意\n上次输出无法解析为 JSON 数组，请只输出纯 JSON 数组，无其他文字。"

    if mem_conn is not None:
        err_run = RefineRunRecorder.start(
            mem_conn, file_id=bucket_key, stage="l1_extraction",
            version=pv.version, variant=pv.variant,
            provider="unavailable", bucket_key=bucket_key,
        )
        RefineRunRecorder.finish(
            mem_conn, err_run, memories_count=0, action_counts={},
            status="error", error=str(last_error),
        )
    raise RefineError(f"L1 提取失败（重试 {max_attempts - 1} 次仍失败）: {last_error}")
