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
VALID_MEMORY_TYPES = {"persona", "episodic", "instruction", "fact"}
VALID_TIME_VELOCITY = {"static", "dynamic"}

# ---------- T-201：类型 ↔ 维度映射硬约束（l1_extraction v007 提示词的校验兜底） ----------

# 单条记忆维度标签上限（提示词要求 1-3 个；实测出现过 4 个，属免费模型执行不稳）
MAX_DIMENSIONS_PER_MEMORY = 3

# episodic 事件类记忆**仅限动态维度**（v007 映射规则）。
# 治理依据：静态维度 ttl_days=null → dream 的 _mark_expired_ttl 天然跳过 →
# 事件流水一旦打静态标签即永久滞留（生产实测 tech_stack 72% 为 episodic/fact）。
EPISODIC_ALLOWED_DYNAMIC_DIMS = {"focus", "goals", "status"}

# 违规降级时的兜底维度（episodic 标签全被剔除后落此）
EPISODIC_FALLBACK_DIM = "status"

# T-201：空结果重试的会话长度门槛（字符）。
# B164 引入「空结果加提示重试 1 次」防本地模型静默漏抽（实测 12.6% 块静默空产出），
# 但 v007 允许输出空数组（A3）后，寒暄/纯执行类短会话也会触发重试 → 每次多花一次调用。
# 故加门槛：会话正文短于此值 → 视为「有意空」，不再重试；长会话仍重试以保住防漏抽收益。
EMPTY_RETRY_MIN_CONV_CHARS = 200

# 违规计数（进程内累计，供测试与运维观测；不写库、不影响落库字段结构，
# 避免重演 B147——新增字段若未同步 refine.py 归一化白名单会被静默丢弃）
_VIOLATION_COUNTER: dict[str, int] = {
    "dim_overflow": 0,       # 维度数超上限被截断
    "episodic_degraded": 0,  # episodic 打静态维度被降级
}


def violation_counts() -> dict[str, int]:
    """返回类型↔维度违规累计计数（只读副本）。"""
    return dict(_VIOLATION_COUNTER)


def reset_violation_counts() -> None:
    """重置违规计数（测试用）。"""
    for k in _VIOLATION_COUNTER:
        _VIOLATION_COUNTER[k] = 0


def _enforce_type_dimension_rule(
    dims: list[str],
    memory_type: str,
    dimensions: list[dict],
) -> tuple[list[str], bool]:
    """执行类型↔维度映射规则，返回 (修正后的维度列表, 是否发生过降级)。

    v007 提示词规定了映射规则，但免费链模型执行不稳（T-136 教训：纯规则 80%，
    加示例才 100%），故在校验层再兜一次底——提示词管引导、代码管兜底，缺一不可。

    规则：
    - persona / instruction / fact → 不限制维度
    - episodic → 仅保留动态维度（registry 的 time_velocity='dynamic'）；
      被剔除后若为空 → 兜底为 status（事件流水归"当前状态"，带 TTL 7d 自动过期）
    - 降级即返回 True，供调用方记 anomaly_warn 计数（可观测）

    维度类别取自注册表（time_velocity 字段），不在此硬编码维度清单。
    """
    if memory_type != "episodic":
        return dims, False

    # 动态维度集合 = id + display_name 双形态：
    # LLM 输出既可能是英文 id（status），也可能是中文展示名（"目标"）——
    # 归一化到 id 发生在 refine.py 之后，此处必须双形态都认，否则会误降级合法标签。
    dynamic_ids: set[str] = set()
    for d in dimensions:
        if d.get("time_velocity") != "dynamic" or d.get("active", 1) != 1:
            continue
        dynamic_ids.add(d["id"])
        display = d.get("display_name")
        if display:
            dynamic_ids.add(str(display).strip())
    allowed = dynamic_ids or set(EPISODIC_ALLOWED_DYNAMIC_DIMS)
    kept = [d for d in dims if d in allowed]
    if kept == dims:
        return dims, False
    if not kept:
        # 全部静态标签被剔除 → 兜底动态维度（仍有 TTL，不会永久滞留）
        return [EPISODIC_FALLBACK_DIM], True
    return kept, True


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
    """从 LLM 输出中提取 JSON 数组（强鲁棒：容忍思考块/代码块/前后废话/截断）。

    本地 Qwen 思考模型可能：① 在 JSON 前后吐 <think:6124c78e>...</think:6124c78e> 推理块；
    ② 用 ```json 包裹；③ 数组后附废话；④ 偶发截断/尾逗号。逐层兜底解析。
    """
    if not text or not text.strip():
        raise ValueError("空输出，无法解析 JSON 数组")
    # 1. 去除 <think:6124c78e>...</think:6124c78e> 思考块（Qwen 思考模型可能泄漏）
    cleaned = re.sub(r"<\s*think\s*>.*?<\s*/\s*think\s*>", "", text,
                     flags=re.DOTALL | re.IGNORECASE)
    # 2. 剥离 markdown 代码块（含 ```json 或纯 ```）
    fm = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    candidate = (fm.group(1) if fm else cleaned).strip()

    attempts = [candidate]
    # 3. 退路 A：去除尾逗号（{,} / [,]）
    attempts.append(re.sub(r",(\s*[}\]])", r"\1", candidate))
    # 4. 退路 B：首个 [ 到最后一个 ]（容忍前后废话）
    s, e = candidate.find("["), candidate.rfind("]")
    if s != -1 and e > s:
        attempts.append(candidate[s:e + 1])

    for cand in attempts:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    # 5. 最后手段：逐对象 regex 提取（容错，最坏丢少量条目）
    objs = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", candidate, re.DOTALL)
    items = []
    for o in objs:
        try:
            items.append(json.loads(o))
        except json.JSONDecodeError:
            continue
    if not items:
        raise ValueError("无法从输出中提取 JSON 数组")
    return items


def _validate_item(item: Any, dimensions: list[dict]) -> dict | None:
    """校验单条记忆，返回规整后的 dict 或 None（无效则跳过）。

    校验规则（spec.md T4）：
    - dimensions 非空
    - priority 0-100 钳制
    - time_velocity ∈ {static, dynamic}（否则按维度默认回填）
    - memory_type ∈ {persona, episodic, instruction, fact}
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
    dims = [str(x) for x in dims]
    memory_type = item.get("memory_type")
    if memory_type not in VALID_MEMORY_TYPES:
        # 不合法类型 → 默认 persona
        memory_type = "persona"

    # T-201：维度数量上限（提示词要求 1-3 个；实测免费模型偶发 4 个）
    if len(dims) > MAX_DIMENSIONS_PER_MEMORY:
        logger.warning(
            "L1 维度标签超上限：%d 个 > %d，截断保留前 %d 个: %r",
            len(dims), MAX_DIMENSIONS_PER_MEMORY, MAX_DIMENSIONS_PER_MEMORY, dims,
        )
        dims = dims[:MAX_DIMENSIONS_PER_MEMORY]
        _VIOLATION_COUNTER["dim_overflow"] += 1

    # T-201：类型 ↔ 维度映射硬约束（v007 提示词规则的代码兜底）
    dims, degraded = _enforce_type_dimension_rule(dims, memory_type, dimensions)
    if degraded:
        logger.warning(
            "L1 类型↔维度违规已降级：memory_type=%s → 维度修正为 %r（episodic 仅限动态维度）",
            memory_type, dims,
        )
        _VIOLATION_COUNTER["episodic_degraded"] += 1
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
    # T-136 原子事实三元组：容错校验（缺键/非字符串/空 → 丢弃该项；非 list → []）
    facts: list[dict] = []
    raw_facts = item.get("facts")
    if isinstance(raw_facts, list):
        for f in raw_facts:
            if not isinstance(f, dict):
                continue
            s = str(f.get("subject") or "").strip()
            p = str(f.get("predicate") or "").strip()
            o = str(f.get("object") or "").strip()
            if s and p and o:
                facts.append({"subject": s, "predicate": p, "object": o})
    return {
        "content": content,
        "dimensions": [str(x) for x in dims],  # 归一化前的原始标签
        "memory_type": memory_type,
        "priority": priority,
        "time_velocity": time_velocity,
        "source_message_ids": source_ids,
        "supersedes": supersedes,
        "facts": facts,
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
    chunk_size: int = 6000,
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
    - 单块 JSON 坏输出重试 1 次（重问 LLM）；**空结果（输出 `[]`）也加提示重试 1 次**，
      仍空则按空块正常返回（2026-09-11：本地模型 12.6% 的块静默空产出，曾致答案块丢失）
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


def _lang_mismatch(conversation: str, memories: list[dict]) -> bool:
    """语言守门：会话以英文（拉丁字母）为主、而记忆以中文为主 → 判为「语言漂移」。

    2026-09-12 LongMemEval 诊断：本地 9B 会把英文会话译成中文记忆（实测约一半记忆
    如此），而英文语料的问题用英文提问 → BM25 完全失配、向量跨语言匹配更弱，检索
    命中率腰斩。检出后由调用方带「必须原文语言」提示重试一次。
    中文会话提炼出中文记忆属正常，不得误报；源会话太短时不下结论。
    """
    def _cjk(s: str) -> int:
        return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")

    def _latin(s: str) -> int:
        return sum(1 for ch in s if ch.isascii() and ch.isalpha())

    src_lat, src_cjk = _latin(conversation), _cjk(conversation)
    if src_lat < 200 or src_lat < src_cjk * 2:
        return False  # 源会话不是「英文为主」（短文本/中文会话）→ 不判
    mem_text = " ".join((m.get("content") or "") for m in memories if isinstance(m, dict))
    if not mem_text.strip():
        return False
    return _cjk(mem_text) > _latin(mem_text)


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
    max_attempts = 2  # 解析/空结果：重试 1 次（B164 既有行为）
    lang_retries = 0
    max_lang_retries = 1  # 语言漂移：额外允许 1 次（2026-09-12）
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + max_lang_retries + 1):
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
        except RefineError as e:
            last_error = e
            logger.warning("L1 输出解析失败 (attempt=%s): %s", attempt, e)
            # 重试时 prompt 加提示
            if attempt < max_attempts:
                prompt = _render_l1_text(pv.text, conversation, dimensions) + \
                    "\n\n# 注意\n上次输出无法解析为 JSON 数组，请只输出纯 JSON 数组，无其他文字。"
            continue

        # 空结果视为可疑：模型可能漏抽整块内容（2026-09-11 实测本地模型 12.6% 的块
        # 静默空产出，其中含答案所在块 → longmemeval recall@8 归零）。
        # 加提示重试一次；仍空则正常返回（空块合法，不抛错）。
        # T-201：短会话（寒暄/单轮播报）的空结果是「有意空」，不再重试（省一次调用）
        conv_chars = len(conversation) if isinstance(conversation, str) else sum(
            len(c) for c in conversation
        )
        if not memories and attempt < max_attempts and conv_chars >= EMPTY_RETRY_MIN_CONV_CHARS:
            logger.warning("L1 空结果 (attempt=%s)，加提示重试", attempt)
            prompt = _render_l1_text(pv.text, conversation, dimensions) + \
                "\n\n# 注意\n上次输出为空数组 []，但对话中可能仍有值得长期保存的记忆。" \
                "请逐条复查对话内容，确保不遗漏任何用户事实（尤其数字、时长、地点、偏好等细节）。" \
                "若确实没有可提炼的记忆，再输出 []。"
            continue

        if attempt > 1:
            logger.info("L1 重试 %s 次成功", attempt)

        # 语言漂移守门（2026-09-12）：英文会话被提炼成中文记忆 → 带语言提示重试（独立预算）
        if lang_retries < max_lang_retries and _lang_mismatch(conversation, memories):
            lang_retries += 1
            logger.warning("L1 语言漂移（英文会话→中文记忆，attempt=%s），加语言提示重试", attempt)
            prompt = _render_l1_text(pv.text, conversation, dimensions) + \
                "\n\n# 注意\n上次输出把会话内容翻译成了中文，这会破坏检索。必须使用**与会话原文相同的语言**输出：" \
                "英文会话 → 英文记忆（逐条改写为英文，专名/数字/术语保持原样，不要意译）；中文会话 → 中文记忆。"
            continue

        if mem_conn is not None:
            run_id = RefineRunRecorder.start(
                mem_conn, file_id=bucket_key, stage="l1_extraction",
                version=pv.version, variant=pv.variant,
                provider=provider_name, bucket_key=bucket_key,
            )
            # T-202 B3：L1 run 补 action 计数采样（衔接提示词分析 P2-2）——
            # extracted/empty 反映提取产出形态；violation_* 为进程内累计口径
            # （_VIOLATION_COUNTER 不写库的设计约束不变，这里只是计数快照），
            # 随时间增长的趋势即可定位 v007 规约的遵守情况。
            vc = violation_counts()
            RefineRunRecorder.finish(
                mem_conn, run_id, memories_count=len(memories),
                action_counts={
                    "extracted": len(memories),
                    "empty": 1 if not memories else 0,
                    "violation_dim_overflow_cum": vc.get("dim_overflow", 0),
                    "violation_episodic_degraded_cum": vc.get("episodic_degraded", 0),
                },
                status="ok", usage=usage,
            )
        meta = {"stage": "l1_extraction", "version": pv.version, "variant": pv.variant}
        logger.info("L1 块完成: version=%s variant=%s memories=%d",
                    pv.version, pv.variant, len(memories))
        return memories, provider_name, meta

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
