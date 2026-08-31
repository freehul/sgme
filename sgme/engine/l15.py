"""engine/l15.py：L1.5 冲突提炼。

流程（§架构约束 7）：
1. 候选池查询：按新记忆分组，标签 OR 预过滤（共享维度的旧记忆，不截断全量召回）
2. 分组分批：按上下文预算（batch_budget tokens）贪心装箱，同一新记忆只进一批；
   仅单记忆候选超上下文预算 → 按 priority 降序 top-k 截断 + anomaly_warn（唯一允许截断场景）
3. prompt 渲染（{{new_memories}} + {{candidates}}）→ LLM → 四动作裁决（批内索引重映射回全局）
4. 四动作落库：
   - store: INSERT 新记忆
   - skip: 不动
   - update: 归档候选行（superseded_by=新id）+ INSERT 新记忆
   - merge: INSERT 合并行（merged_content + 时间戳并集）+ 归档所有命中候选

TTL 字段按维度默认回填（ttl_days=None 时取维度默认）。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sgme import config
from sgme.engine import normalize
from sgme.llm import chain as llm_chain
from sgme.llm import provider as llm_provider
from sgme.prompts import BucketCtx, PromptStore
from sgme.data import memory_dao
from sgme.data.search import vector as vector_mod

logger = logging.getLogger("sgme.engine.l15")

# 候选池 top-k 截断阈值（单记忆候选超上下文预算 → 截断 + anomaly_warn，铁律 #7 唯一允许截断场景）
DEFAULT_TOP_K = 50
# build_candidate_pool 直调（无上下文预算）时的默认单记忆候选字符预算（兼容旧参数语义）
DEFAULT_CHAR_BUDGET = 24000
# 字符→token 估算比（CJK ≈ 1 字符/token，英文 ≈ 4 字符/token，取保守折中）
CHARS_PER_TOKEN = 2.0
# 每批 prompt 模板 + 指令固定开销（tokens，估算）
TEMPLATE_OVERHEAD_TOKENS = 1500
# 候选/新记忆格式化行开销（字符，估算）
FORMAT_CHARS_PER_CANDIDATE = 120
FORMAT_CHARS_PER_NEW_MEMORY = 160
# batch_budget 计算失败时的兜底预算（tokens）
DEFAULT_BATCH_BUDGET = 24000
# 候选池全量召回上限（铁律 #7 不截断）：data 层 LIMIT 参数不接受 NULL，
# 用最大有符号整数等效「不限」；万级候选池远达不到该值
_UNLIMITED_RECALL_LIMIT = 2**31 - 1


class L15Error(Exception):
    """L1.5 冲突提炼失败。"""


@dataclass
class RelationEdge:
    """T-135 语义边：L1.5 裁决附带的新记忆 ↔ 候选关系判定（零新增调用）。"""
    candidate_id: str               # 候选 memory_id（旧记忆）
    relation: str                   # similar / causes / contradicts
    confidence: float               # LLM 置信（0.0-1.0），过滤阈值见 l15.semantic_edges.min_weight


@dataclass
class ConflictDecision:
    """单条新记忆的冲突裁决。"""
    new_memory_index: int           # 新记忆在输入列表中的序号（0-based）
    candidate_ids: list[str]        # 命中的候选 memory_id 列表
    action: str                     # store/skip/update/merge
    merged_content: str | None = None
    reason: str = ""
    relations: list[RelationEdge] = field(default_factory=list)   # T-135：关系判定（可选）


@dataclass
class L15Result:
    """L1.5 提炼结果。"""
    stored: list[str] = field(default_factory=list)        # 新写入的 memory_id
    skipped: list[int] = field(default_factory=list)       # skip 的新记忆 index
    updated: list[str] = field(default_factory=list)       # update 产生的新 memory_id
    merged: list[str] = field(default_factory=list)        # merge 产生的新 memory_id
    archived: list[str] = field(default_factory=list)      # 被归档的旧 memory_id
    anomaly_warn: bool = False
    error: str | None = None
    prompt_meta: dict | None = None                         # #33：本次 L1.5 提示词版本元信息
    prescreen_skipped: int = 0                              # T-132：本批因 embed 不可达走 skip_conflict 降级的新记忆数（可观测标记）
    semantic_edges_written: int = 0                         # T-135：本次落库写入的语义边数（source='l1_conflict'）


# ---------- prompt 渲染 ----------

def render_l15(new_memories: list[dict], candidates: list[dict], ctx: BucketCtx | None = None) -> str:
    """渲染 L1.5 冲突提炼 prompt（模板经 PromptStore 读取，支持 A/B 与钉版）。

    - {{new_memories}}: 新记忆列表（带 index）
    - {{candidates}}: 候选记忆列表（带 id）
    """
    template = PromptStore().get("l1_conflict", ctx).text
    return _render_l15_text(template, new_memories, candidates)


def _render_l15_text(template: str, new_memories: list[dict], candidates: list[dict]) -> str:
    """渲染已读出的模板文本（{{new_memories}} + {{candidates}}）。"""
    nm_text = _format_new_memories(new_memories)
    cand_text = _format_candidates(candidates)
    return template.replace("{{new_memories}}", nm_text).replace("{{candidates}}", cand_text)


def _format_new_memories(new_memories: list[dict]) -> str:
    lines = []
    for i, m in enumerate(new_memories):
        lines.append(f"[新记忆#{i}] content: {m['content']}")
        lines.append(f"  dimensions: {m.get('dimension_ids', m.get('dimensions', []))}")
        lines.append(f"  memory_type: {m.get('memory_type', 'persona')}")
        lines.append(f"  priority: {m.get('priority', 50)}")
        lines.append(f"  time_velocity: {m.get('time_velocity', 'static')}")
        lines.append("")
    return "\n".join(lines)


def _format_candidates(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        lines.append(f"[候选#{c['memory_id']}] content: {c['content']}")
        lines.append(f"  dimensions: {c.get('tags', [])}")
        lines.append(f"  memory_type: {c.get('memory_type', 'persona')}")
        lines.append(f"  priority: {c.get('priority', 50)}")
        lines.append(f"  updated_at: {c.get('updated_at', '')}")
        lines.append("")
    return "\n".join(lines)


# ---------- JSON 解析 ----------

#: T-135 语义边允许的关系类型 + source 标识（可溯源，delete_edges_by_source 一键关闭）
SEMANTIC_RELATIONS: tuple[str, ...] = ("similar", "causes", "contradicts")
SEMANTIC_EDGE_SOURCE = "l1_conflict"


def _parse_relations(raw: object) -> list[RelationEdge]:
    """容错解析 LLM 输出的 relations 数组（T-135）。

    - 非 list / 字段缺失 / 类型错误 → 跳过该项（不阻断裁决，宁缺毋滥）
    - relation 不在允许集合 → 丢弃
    """
    if not isinstance(raw, list):
        return []
    out: list[RelationEdge] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        cand = r.get("candidate_id")
        rel = r.get("relation")
        if not isinstance(cand, str) or not cand:
            continue
        if not isinstance(rel, str) or rel not in SEMANTIC_RELATIONS:
            continue
        conf = r.get("confidence")
        try:
            conf_f = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            continue  # 置信度无法解析 → 宁缺毋滥，丢弃该项
        conf_f = min(max(conf_f, 0.0), 1.0)
        out.append(RelationEdge(candidate_id=cand, relation=rel, confidence=conf_f))
    return out


def parse_l15_output(text: str) -> list[ConflictDecision]:
    """解析 L1.5 输出为裁决列表。"""
    import re
    text = text.strip()
    # 去除 ```json 包裹
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            raise L15Error(f"JSON 解析失败: {text[:200]}")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise L15Error(f"期望 JSON 数组，得到 {type(data).__name__}")
    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        action = item.get("action", "store")
        if action not in ("store", "skip", "update", "merge"):
            action = "store"
        result.append(ConflictDecision(
            new_memory_index=int(item.get("new_memory_index", 0)),
            candidate_ids=[str(x) for x in item.get("candidate_ids", [])],
            action=action,
            merged_content=item.get("merged_content"),
            reason=item.get("reason", ""),
            relations=_parse_relations(item.get("relations")),
        ))
    return result


# ---------- 候选池查询（铁律 #7：不截断全量召回，按新记忆分组） ----------

@dataclass
class CandidateGroup:
    """单条新记忆的候选池分组（铁律 #7：同一新记忆只进一批的最小单元）。

    - candidates：与该新记忆共享任一维度（OR 预过滤）的旧记忆，全量召回，priority 降序
    - truncated：单组候选超上下文预算 → 已 top-k 截断（记 anomaly_warn）
    """
    new_memory_index: int
    new_memory: dict
    candidates: list[dict]
    truncated: bool = False


def estimate_group_tokens(
    group: CandidateGroup,
    chars_per_token: float = CHARS_PER_TOKEN,
    chars_per_candidate: int = FORMAT_CHARS_PER_CANDIDATE,
    chars_per_new_memory: int = FORMAT_CHARS_PER_NEW_MEMORY,
) -> int:
    """估算一个候选分组占用的上下文 tokens（内容 + prompt 格式化开销）。"""
    cand_chars = sum(len(c.get("content", "") or "") for c in group.candidates)
    cand_chars += chars_per_candidate * len(group.candidates)
    new_chars = len(group.new_memory.get("content", "") or "") + chars_per_new_memory
    return math.ceil((cand_chars + new_chars) / chars_per_token)


def _memory_tags(mem_conn: sqlite3.Connection, memory_id: str, cache: dict[str, list[str]]) -> list[str]:
    """取记忆 tags（批量查询缓存，避免 N+1）。"""
    if memory_id not in cache:
        mem = memory_dao.get_memory(mem_conn, memory_id)
        cache[memory_id] = mem.get("tags", []) if mem else []
    return cache[memory_id]


#: 预筛降级哨兵：embed 不可达且 fallback=skip_conflict 时的返回标记。
#: 调用方（build_candidate_groups）识别后清空该新记忆候选 → resolve_conflicts
#: 短路直接 store（零 LLM 调用）。区别于 None（回退全量召回，宁贵勿漏）。
PRESCREEN_SKIP_CONFLICT = "__PRESCREEN_SKIP_CONFLICT__"


def _record_prescreen_skip_run(
    mem_conn: sqlite3.Connection,
    bucket_key: str,
    count: int,
    bucket_ctx,
) -> None:
    """T-132：预筛降级可观测标记。

    embed 不可达且 fallback=skip_conflict 时，候选被清空、短路直接 store，
    本就不调 LLM、不走正常裁决路径 → 原 action_counts["skip"]（LLM 判无变化）
    与之同名混淆、且降级完全不可见。此处独立记一条 refine_run，
    action_counts={"prescreen_skipped": count}，供 A/B 观测 / metrics 端点识别。
    """
    from sgme.data.refine_dao import RefineRunRecorder

    pv = PromptStore().get("l1_conflict", bucket_ctx)
    run_id = RefineRunRecorder.start(
        mem_conn, file_id=bucket_key, stage="l1_conflict",
        version=pv.version, variant=pv.variant,
        provider="prescreen_skip", bucket_key=bucket_key,
    )
    RefineRunRecorder.finish(
        mem_conn, run_id, memories_count=count,
        action_counts={"prescreen_skipped": count}, status="ok", error=None,
    )


def _build_prescreened_candidates(
    mem_conn: sqlite3.Connection,
    new_mem: dict,
    cfg: dict,
    prescreen: dict,
    tags_cache: dict[str, list[str]],
    dims: list[str],
) -> list[dict] | None | str:
    """向量预筛候选：向量 Top-K ∪ 维度 Top-N（priority 降序），按 memory_id 去重。

    2026-08-12 成本治理：记忆库 9k+ 时维度 OR 全量召回使单次 l1_conflict 消耗
    67-100 万 tokens。预筛把单记忆候选限制在 vector_top_k + dimension_top_n
    （默认 50+50），prompt 降到 ~2 万 tokens（降 98%）。

    返回语义（2026-08-16 T-4x 成本治理，embed 不可达时的策略分流）：
    - list[dict]：预筛成功，候选 = 向量 Top-K ∪ 维度 Top-N
    - None：embed 不可达，fallback=full_recall → 调用方回退维度 OR 全量召回
      （历史行为，宁贵勿漏；08-11/12 单日 9800 万 tokens 的元凶）
    - PRESCREEN_SKIP_CONFLICT：embed 不可达，fallback=skip_conflict → 调用方
      清空该新记忆候选，resolve_conflicts 短路直接 store（零 LLM 调用）。
      向量链路异常时召回质量不可信，先保数据落地，事后补检。
    """
    vector_top_k = int(prescreen.get("vector_top_k", DEFAULT_TOP_K))
    dimension_top_n = int(prescreen.get("dimension_top_n", DEFAULT_TOP_K))
    fallback = prescreen.get("fallback", "full_recall")

    # 1. 向量 Top-K（语义候选，可命中维度不重叠的记忆）
    try:
        vec = vector_mod.embed((new_mem.get("content", "") or ""), cfg)
        if vec is None:
            if fallback == "skip_conflict":
                logger.warning("L1.5 向量预筛降级：embedding 端点不可达，fallback=skip_conflict 跳过冲突检测")
                return PRESCREEN_SKIP_CONFLICT
            logger.warning("L1.5 向量预筛降级：embedding 端点不可达，回退全量召回（fallback=full_recall）")
            return None
        vec_cands = vector_mod.vector_search(mem_conn, vec, limit=vector_top_k)
    except Exception as e:
        if fallback == "skip_conflict":
            logger.warning("L1.5 向量预筛异常，fallback=skip_conflict 跳过冲突检测: %s", e)
            return PRESCREEN_SKIP_CONFLICT
        logger.warning("L1.5 向量预筛异常，回退全量召回: %s", e)
        return None

    # 2. 维度 OR 候选（现状召回语义），截断到 dimension_top_n（priority 降序）
    dim_cands: list[dict] = []
    if dims:
        dim_cands = memory_dao.list_memories_by_dimension(
            mem_conn, list(dims), match="any",
            limit=_UNLIMITED_RECALL_LIMIT, include_expired=True,
        )
        dim_cands.sort(key=lambda c: (c.get("priority", 0) or 0), reverse=True)
        dim_cands = dim_cands[:dimension_top_n]

    # 3. 并集去重（向量候选在前，维度候选补充）
    merged: list[dict] = []
    seen: set[str] = set()
    for c in vec_cands:
        if c["memory_id"] not in seen:
            seen.add(c["memory_id"])
            c["tags"] = _memory_tags(mem_conn, c["memory_id"], tags_cache)
            merged.append(c)
    for c in dim_cands:
        if c["memory_id"] not in seen:
            seen.add(c["memory_id"])
            c["tags"] = _memory_tags(mem_conn, c["memory_id"], tags_cache)
            merged.append(c)
    return merged


def build_candidate_groups(
    mem_conn: sqlite3.Connection,
    new_memories: list[dict],
    top_k: int = DEFAULT_TOP_K,
    per_memory_budget_tokens: int | None = None,
    cfg: dict | None = None,
    prescreen: dict | None = None,
    stats: dict | None = None,
) -> tuple[list[CandidateGroup], bool]:
    """按新记忆分组构建候选池：标签 OR 预过滤 + 全量召回 + 单记忆预算 top-k。

    铁律 #7 语义（2026-08-11 T-18 治理，替代 B2 遗留的整池 char_budget 截断）：
    - 不截断全量召回：每组候选 = 与该新记忆共享任一维度的全部旧记忆（等效 LIMIT 无限）
    - 仅单记忆候选超上下文预算 → 按 priority 降序 top-k 截断 + truncated（anomaly_warn）
    - 分组即批边界的最小单元：同一新记忆只进一批

    向量预筛（2026-08-12 成本治理，prescreen 配置）：
    - prescreen.enabled=true 且向量可用 → 候选 = 向量 Top-K ∪ 维度 Top-N（见
      _build_prescreened_candidates），单记忆候选 ≤ vector_top_k + dimension_top_n
    - embed 不可达 / 向量检索异常 → 自动回退全量召回（宁贵勿漏）
    - prescreen 未配置或 enabled=false → 完全现状（全量召回 + 预算 top-k）

    per_memory_budget_tokens=None → 用 DEFAULT_CHAR_BUDGET 字符预算折算（兼容直调入口）。
    返回 (groups, any_truncated)。

    stats（可选）：调用方传入的 dict，本函数填充 ``prescreen_skipped``（因 embed
    不可达走 skip_conflict 降级、清空候选的新记忆数）——T-132 预筛降级可观测标记。
    """
    if per_memory_budget_tokens is None:
        per_memory_budget_tokens = int(DEFAULT_CHAR_BUDGET / CHARS_PER_TOKEN)
    tags_cache: dict[str, list[str]] = {}
    groups: list[CandidateGroup] = []
    any_truncated = False
    prescreen_skipped = 0
    for i, new_mem in enumerate(new_memories):
        dims = new_mem.get("dimension_ids", new_mem.get("dimensions", []))
        cands: list[dict] = []
        prescreen_used = False
        if prescreen and cfg and prescreen.get("enabled", False):
            pc = _build_prescreened_candidates(mem_conn, new_mem, cfg, prescreen, tags_cache, dims)
            if pc is PRESCREEN_SKIP_CONFLICT:
                # fallback=skip_conflict：向量链路异常 → 清空候选（无候选 = 短路直接 store，
                # resolve_conflicts 不调 LLM），保数据落地不烧全量召回的钱
                logger.warning(
                    "L1.5 预筛跳过冲突检测（fallback=skip_conflict）: 新记忆#%d 直接 store", i,
                )
                prescreen_used = True
                prescreen_skipped += 1
            elif pc is not None:
                cands = pc
                prescreen_used = True
        if not prescreen_used and dims:
            cands = memory_dao.list_memories_by_dimension(
                mem_conn, list(dims), match="any",
                # 全量召回不截断（铁律 #7）：等效 LIMIT 无限
                limit=_UNLIMITED_RECALL_LIMIT, include_expired=True,
            )
            # 候选按 priority 降序：超预算截断时优先保留高价值候选
            cands.sort(key=lambda c: (c.get("priority", 0) or 0), reverse=True)
            for c in cands:
                c["tags"] = _memory_tags(mem_conn, c["memory_id"], tags_cache)
        truncated = False
        group = CandidateGroup(i, new_mem, cands)
        if cands and estimate_group_tokens(group) > per_memory_budget_tokens:
            logger.warning(
                "L1.5 单记忆候选 %d 条超上下文预算（%d tokens）→ top-%d 截断 + anomaly_warn",
                len(cands), per_memory_budget_tokens, top_k,
            )
            group.candidates = cands[:top_k]
            group.truncated = True
            any_truncated = True
        groups.append(group)
    if stats is not None:
        stats["prescreen_skipped"] = prescreen_skipped
    return groups, any_truncated


def build_candidate_pool(
    mem_conn: sqlite3.Connection,
    new_memories: list[dict],
    top_k: int = DEFAULT_TOP_K,
    char_budget: int | None = None,
    cfg: dict | None = None,
    prescreen: dict | None = None,
) -> tuple[list[dict], bool]:
    """构建候选池（兼容入口）：按新记忆分组全量召回，合并去重返回。

    语义（铁律 #7，2026-08-11 T-18 治理）：
    - 不截断全量召回：不再对整池做 char_budget 截断（旧 B2 实现违背铁律，可能漏冲突）
    - char_budget 仅作用于单记忆候选：某条新记忆的候选超预算 → top-k 截断 + anomaly_warn
    - prescreen 配置（2026-08-12 成本治理）→ 透传 build_candidate_groups 向量预筛

    返回 (候选列表, anomaly_warn)。
    """
    per_memory_tokens = None
    if char_budget is not None:
        per_memory_tokens = int(char_budget / CHARS_PER_TOKEN)
    groups, warn = build_candidate_groups(
        mem_conn, new_memories, top_k=top_k, per_memory_budget_tokens=per_memory_tokens,
        cfg=cfg, prescreen=prescreen,
    )
    pool: list[dict] = []
    seen: set[str] = set()
    for g in groups:
        for c in g.candidates:
            if c["memory_id"] not in seen:
                seen.add(c["memory_id"])
                pool.append(c)
    return pool, warn


# ---------- 分批（铁律 #7：按上下文预算分批，同一新记忆只进一批） ----------

@dataclass
class Batch:
    """一批送检单元：若干新记忆（各带自己的候选）→ 一次 LLM 裁决。

    - start_index：本批第一条新记忆在全局 new_memories 中的下标（裁决索引重映射）
    - candidates：批内候选并集（按新记忆顺序去重，全部候选均参与比对）
    """
    new_memories: list[dict]
    start_index: int
    candidates: list[dict]


def _merge_group_candidates(groups: list[CandidateGroup]) -> list[dict]:
    """合并多个分组的候选（按组顺序去重，同一候选只出现一次）。"""
    merged: list[dict] = []
    seen: set[str] = set()
    for g in groups:
        for c in g.candidates:
            if c["memory_id"] not in seen:
                seen.add(c["memory_id"])
                merged.append(c)
    return merged


def build_batches(
    groups: list[CandidateGroup],
    batch_budget: int,
    chars_per_token: float = CHARS_PER_TOKEN,
    template_overhead_tokens: int = TEMPLATE_OVERHEAD_TOKENS,
) -> list[Batch]:
    """按上下文预算分批：同一新记忆只进一批，每批不超过预算。

    贪心装箱（保持新记忆顺序）：
    - 每组（新记忆 + 其全部候选）整体放入一批，不拆散 → 同一新记忆只进一批
    - 当前批累积（模板开销 + 各组估算）再加下组超预算 → 开新批
    - 单组自身超预算（新记忆内容极大）→ 单独成批不丢弃（宁超不丢，记日志）
    """
    batches: list[Batch] = []
    if not groups:
        return batches
    cur_groups: list[CandidateGroup] = []
    cur_cost = 0
    cur_start = groups[0].new_memory_index

    def flush() -> None:
        nonlocal cur_groups, cur_cost
        if cur_groups:
            batches.append(Batch(
                new_memories=[g.new_memory for g in cur_groups],
                start_index=cur_start,
                candidates=_merge_group_candidates(cur_groups),
            ))
        cur_groups = []
        cur_cost = 0

    for g in groups:
        cost = estimate_group_tokens(g, chars_per_token)
        if cur_groups and template_overhead_tokens + cur_cost + cost > batch_budget:
            flush()
            cur_start = g.new_memory_index
        if cost > batch_budget and not cur_groups:
            # 单组自身超预算：独立成批不丢弃（每批不超过预算的例外，宁超不丢）
            logger.warning(
                "L1.5 单组（新记忆#%d）估算 %d tokens 超批预算 %d → 独立成批不丢弃",
                g.new_memory_index, cost, batch_budget,
            )
        cur_groups.append(g)
        cur_cost += cost
    flush()
    return batches


# ---------- TTL 回填 ----------

def _backfill_ttl(ttl_days: int | None, dimension_ids: list[str], dimensions: list[dict]) -> int | None:
    """TTL 字段按维度默认回填：ttl_days=None 时取维度默认。

    创意池铁律（T-26，2026-08-13 强化）：dimension_ids 含 ``ideas`` → 强制 None——
    创意长期保存，覆盖其他维度 TTL（否则 ideas+goals/projects 共存的创意会取 90d，
    90 天后过期退出注入，违背创意池「ideas + ttl_days=NULL」定义）。
    """
    if "ideas" in dimension_ids:
        return None
    if ttl_days is not None:
        return ttl_days
    dim_map = {d["id"]: d for d in dimensions}
    # 任一动态维度有 ttl → 取该 ttl
    for dim_id in dimension_ids:
        d = dim_map.get(dim_id)
        if d and d.get("ttl_days"):
            return d["ttl_days"]
    return None


# ---------- 四动作落库 ----------

def _store_memory(
    mem_conn: sqlite3.Connection,
    new_mem: dict,
    dimensions: list[dict],
    source_ref: str | None = None,
    prompt_version: str | None = None,
) -> str:
    """store 动作：INSERT 新记忆。"""
    ttl = _backfill_ttl(
        new_mem.get("ttl_days"),
        new_mem.get("dimension_ids", new_mem.get("dimensions", [])),
        dimensions,
    )
    sources = [(source_ref, "session")] if source_ref else None
    return memory_dao.insert_memory(
        mem_conn,
        content=new_mem["content"],
        memory_type=new_mem.get("memory_type", "persona"),
        priority=new_mem.get("priority", 50),
        time_velocity=new_mem.get("time_velocity", "static"),
        ttl_days=ttl,
        dimension_ids=new_mem.get("dimension_ids", new_mem.get("dimensions", [])),
        sources=sources,
        agent_tag=new_mem.get("agent_tag"),
        created_at=new_mem.get("created_at"),
        updated_at=new_mem.get("updated_at"),
        prompt_version=prompt_version,
        occurred_at=new_mem.get("occurred_at"),
        facts=new_mem.get("facts"),
    )


def _update_memory(
    mem_conn: sqlite3.Connection,
    new_mem: dict,
    candidate_id: str,
    dimensions: list[dict],
    source_ref: str | None = None,
    prompt_version: str | None = None,
) -> str:
    """update 动作：归档候选行 + INSERT 新记忆。"""
    new_id = _store_memory(mem_conn, new_mem, dimensions, source_ref, prompt_version=prompt_version)
    memory_dao.archive_memory(mem_conn, candidate_id, superseded_by=new_id)
    return new_id


def _merge_memory(
    mem_conn: sqlite3.Connection,
    new_mem: dict,
    candidate_ids: list[str],
    merged_content: str,
    dimensions: list[dict],
    source_ref: str | None = None,
    prompt_version: str | None = None,
) -> str:
    """merge 动作：INSERT 合并行 + 归档所有命中候选。

    合并行：
    - content = merged_content
    - updated_at = 候选 + 新记忆时间戳并集（取最大）
    - priority = 新记忆 priority（或可提升，简单实现取新值）
    """
    # 取候选中最大 updated_at 作为合并行 updated_at（时间戳并集 = 取最大）
    max_updated = new_mem.get("updated_at")
    # v0.5：occurred_at 同样取并集最大值（会话真实发生时刻）
    max_occurred = new_mem.get("occurred_at")
    for cid in candidate_ids:
        cand = memory_dao.get_memory(mem_conn, cid)
        if cand and cand.get("updated_at"):
            if not max_updated or cand["updated_at"] > max_updated:
                max_updated = cand["updated_at"]
        if cand and cand.get("occurred_at"):
            if not max_occurred or cand["occurred_at"] > max_occurred:
                max_occurred = cand["occurred_at"]

    merged_mem = {
        **new_mem,
        "content": merged_content,
        "updated_at": max_updated,
        "occurred_at": max_occurred,
    }
    new_id = _store_memory(mem_conn, merged_mem, dimensions, source_ref, prompt_version=prompt_version)
    # 归档所有命中候选
    for cid in candidate_ids:
        memory_dao.archive_memory(mem_conn, cid, superseded_by=new_id)
    return new_id


# ---------- 替代联动（ST-18：L1 supersedes 声明 → 旧主体记忆标记） ----------

# 主体名最小长度（防御 LLM 输出空串/单字符，如「站」会大面积误伤）
MIN_SUBJECT_LEN = 2
# reject_reason 中替代者内容截断长度（保持 reason 简洁可读）
REASON_CONTENT_MAX = 100


def _normalize_subjects(supersedes) -> list[str]:
    """规整 L1 supersedes 字段为主体名列表（str → 单元素；list → 过滤空白/过短/重复）。"""
    if isinstance(supersedes, str):
        raw = [supersedes]
    elif isinstance(supersedes, list):
        raw = supersedes
    else:
        raw = []
    subjects: list[str] = []
    for s in raw:
        if not isinstance(s, str):
            continue
        s = s.strip()
        if len(s) >= MIN_SUBJECT_LEN and s not in subjects:
            subjects.append(s)
    return subjects


def _content_mentions(content: str, subject: str) -> bool:
    """content 是否提及主体名（大小写不敏感子串匹配，与 AIXM 清理脚本口径一致）。"""
    return subject.lower() in (content or "").lower()


def _build_reject_reason(new_content: str, new_id: str) -> str:
    """构造替代联动 reject_reason：记录替代者（新记忆内容）与溯源链（新 memory_id）。"""
    snippet = (new_content or "").strip()
    if len(snippet) > REASON_CONTENT_MAX:
        snippet = snippet[:REASON_CONTENT_MAX] + "…"
    return f"已被「{snippet}」替代（supersession 联动，替代记忆 {new_id}）"


def apply_supersession_linkage(
    mem_conn: sqlite3.Connection,
    new_memories: list[dict],
) -> list[str]:
    """L1 supersedes 替代联动（ST-18）：新记忆声明「X 已被 Y 替代」→ 旧主体 X 相关记忆自动标记。

    触发：L1 提炼输出带 supersedes 字段（提示词约定，见 prompts/l1_extraction.txt），
    表示该记忆的主体 Y 已取代旧主体 X。**在 L1.5 落库之后**（新记忆已带 memory_id）
    调用本函数，把仍 active 且内容提及 X 的旧记忆标记 status='rejected'（数据保留
    可溯源，可 unreject 恢复），reject_reason 记录替代者 Y（新记忆内容）与新记忆
    memory_id（溯源链）。标记后的记忆退出候选池/搜索/注入（既有过滤路径），
    防过时记忆长期存活（2026-08-11 AIXM 案例）。

    匹配口径（与 L1.5 候选池同构，铁律 #7 标签 OR 预过滤）：
    - 召回：与新记忆共享任一维度的全部非 rejected 记忆（含 TTL 过期，全量不截断）
    - 过滤：status='active' 且 content 包含主体名（大小写不敏感子串匹配）
    - 排除：本批新记忆自身（其 content 必然提及旧主体名，如「X 已被 Y 替代」）

    与现有 Supersession（memory_archive）机制协同不冲突（判等锚点 memory_id）：
    - 在 L1.5 落库之后运行：被 update/merge 归档的记忆已不在 memories 表，
      不会被重复处理；本函数也不触碰 memory_archive 表
    - 标记后的记忆 status='rejected'，天然退出后续候选池（status != 'rejected'），
      不会再被 L1.5 update/merge 归档 → 与 archive 链互不干扰

    返回被标记的旧 memory_id 列表（去重）。
    """
    batch_ids = {m.get("memory_id") for m in new_memories if m.get("memory_id")}
    rejected: list[str] = []
    seen: set[str] = set()
    for new_mem in new_memories:
        new_id = new_mem.get("memory_id")
        subjects = _normalize_subjects(new_mem.get("supersedes"))
        if not new_id or not subjects:
            continue
        dims = new_mem.get("dimension_ids", new_mem.get("dimensions", []))
        candidates: list[dict] = []
        if dims:
            candidates = memory_dao.list_memories_by_dimension(
                mem_conn, list(dims), match="any",
                limit=_UNLIMITED_RECALL_LIMIT, include_expired=True,
            )
        for subject in subjects:
            for cand in candidates:
                if cand["memory_id"] in batch_ids:
                    # 本批新记忆（含声明者自身）：刚经 L1.5 裁决落库，不标记
                    continue
                if cand.get("status") != "active":
                    continue
                if not _content_mentions(cand.get("content", ""), subject):
                    continue
                if cand["memory_id"] in seen:
                    continue
                seen.add(cand["memory_id"])
                reason = _build_reject_reason(new_mem.get("content", ""), new_id)
                if memory_dao.reject_memory(mem_conn, cand["memory_id"], reason):
                    rejected.append(cand["memory_id"])
    return rejected


# ---------- 完整 L1.5 提炼 ----------

def _write_semantic_edges(
    mem_conn: sqlite3.Connection,
    new_mem: dict,
    decision: ConflictDecision,
    cfg: dict,
) -> int:
    """T-135：把 L1.5 裁决附带的关系判定写入 memory_edges（source='l1_conflict'）。

    零新增调用：关系判定复用冲突提炼同一次 LLM 输出（relations 字段）。
    脏边控制（AC）：
    - weight 阈值：confidence < `l15.semantic_edges.min_weight`（默认 0.6）丢弃
    - 被 update/merge 归档的候选跳过（已被取代，替代关系由 archive 链 supersedes 承载）
    - 候选非 active（rejected/过期）跳过
    - source='l1_conflict' 可溯源：delete_edges_by_source(conn, 'l1_conflict') 一键关闭该路
    返回写入边数。
    """
    from sgme.data import edge_dao
    se_cfg = (cfg.get("l15", {}) or {}).get("semantic_edges", {}) or {}
    if not se_cfg.get("enabled", True):
        return 0
    min_weight = float(se_cfg.get("min_weight", 0.6))
    new_id = new_mem.get("memory_id")
    if not new_id or not decision.relations:
        return 0
    archived = set(decision.candidate_ids)
    written = 0
    for rel in decision.relations:
        if rel.candidate_id in archived:
            continue
        if rel.confidence < min_weight:
            continue
        cand = memory_dao.get_memory(mem_conn, rel.candidate_id)
        if not cand or cand.get("status") != "active":
            continue
        edge_dao.create_edge(
            mem_conn, new_id, rel.candidate_id, rel.relation,
            weight=round(rel.confidence, 3), source=SEMANTIC_EDGE_SOURCE,
        )
        written += 1
    # create_edge 是裸 INSERT（隐式事务）：必须提交，否则下一条 insert_memory 的
    # BEGIN 会抛 "cannot start a transaction within a transaction"（多记忆批次实测）
    if written:
        mem_conn.commit()
    return written


def _resolve_batch_budget(llm_cfg: dict) -> int:
    """计算 L1.5 上下文预算（tokens）：链首节点 context_window 折算；失败回退默认值。"""
    try:
        first_node = llm_cfg["chains"]["refinement"][0]
        return llm_chain.batch_budget(first_node, llm_cfg.get("rules", {}))
    except Exception:
        logger.warning("batch_budget 计算失败，回退默认预算 %d tokens", DEFAULT_BATCH_BUDGET)
        return DEFAULT_BATCH_BUDGET


def resolve_conflicts(
    new_memories: list[dict],
    mem_conn: sqlite3.Connection,
    cfg: dict,
    client=None,
    top_k: int = DEFAULT_TOP_K,
    source_ref: str | None = None,
    bucket_ctx: BucketCtx | None = None,
    prompt_version: str | None = None,
) -> L15Result:
    """L1.5 冲突提炼：候选池（分组全量召回）→ 预算分批 → LLM 裁决 → 四动作落库。

    铁律 #7（T-18 治理）：
    - 候选池按新记忆分组全量召回（不整池截断），同一新记忆只进一批
    - 仅单记忆候选超上下文预算 → top-k 截断 + anomaly_warn
    - 无候选的新记忆不进 LLM 批 → 落库阶段默认 store（短路到单记忆粒度）

    - bucket_ctx：A/B 分流上下文（默认 file_id）
    - prompt_version：产出这些新记忆的 L1 版本（形如 `l1_extraction:v002`），
      随 store/update/merge 写入 memories.prompt_version（#33 透传）
    - 每候选批记录一条 refine_run（action 分布 + provider + version/variant）
    - 返回 L15Result。
    """
    from sgme.data.refine_dao import RefineRunRecorder

    result = L15Result()
    if not new_memories:
        return result

    # 0. 幂等预检（2026-08-22 修复）：同一 source_ref 重试抽出的「同源 + 同内容」记忆
    #    直接跳过落库（复用既有 active 记忆），杜绝重试造重复。确定性守卫，不依赖 LLM。
    #    source_ref 一个文件所有记忆共享 → 命中即「本文件此前已提炼出同一条记忆」。
    #    无 source_ref（异常路径）或不命中 → idem_skip 为空，行为完全不变。
    idem_skip: set[int] = set()
    if source_ref:
        for i, nm in enumerate(new_memories):
            existing = memory_dao.find_active_by_source_ref_content(
                mem_conn, source_ref, nm.get("content", "")
            )
            if existing:
                idem_skip.add(i)
                logger.info(
                    "L1.5 幂等跳过: new_memory#%d content 已存在 %s（source_ref=%s）",
                    i, existing, source_ref,
                )

    dimensions = cfg["dimensions"]
    llm_cfg = cfg["llm"]
    bucket_key = bucket_ctx.bucket_key if (bucket_ctx and bucket_ctx.bucket_key) else "unknown"

    # 1. 上下文预算（tokens，链首节点 context_window 折算；失败回退默认）
    budget = _resolve_batch_budget(llm_cfg)

    # 1.5 候选池（铁律 #7：按新记忆分组全量召回；
    #     仅单记忆候选超上下文预算 → top-k 截断 + anomaly_warn；
    #     2026-08-12 成本治理：l15.prescreen 配置开启向量预筛 → 候选受限，见 build_candidate_groups）
    prescreen = (cfg.get("l15", {}) or {}).get("prescreen")
    _prescreen_stats: dict = {}
    groups, pool_warn = build_candidate_groups(
        mem_conn, new_memories, top_k=top_k, per_memory_budget_tokens=budget,
        cfg=cfg, prescreen=prescreen, stats=_prescreen_stats,
    )
    prescreen_skipped = int(_prescreen_stats.get("prescreen_skipped", 0))
    result.prescreen_skipped = prescreen_skipped
    result.anomaly_warn = pool_warn

    # 1.6 所有候选池为空 → 无冲突可能，直接全部 store（短路，不调 LLM）
    # 否则空 {{candidates}} 会让 LLM 困惑（输出非 JSON 文本），且浪费一次调用
    if not any(g.candidates for g in groups):
        logger.info("L1.5 候选池为空，全部 store（%d 条，短路）", len(new_memories))
        # T-132：若因预筛降级清空候选，独立记一条 refine_run 标记（否则完全不可见）
        if prescreen_skipped > 0:
            _record_prescreen_skip_run(mem_conn, bucket_key, prescreen_skipped, bucket_ctx)
        for i, new_mem in enumerate(new_memories):
            if i in idem_skip:
                result.skipped.append(i)
                continue
            new_id = _store_memory(mem_conn, new_mem, dimensions, source_ref, prompt_version=prompt_version)
            new_mem["memory_id"] = new_id
            result.stored.append(new_id)
        return result

    # 2. 分批（铁律 #7：按上下文预算分批，同一新记忆只进一批）
    # 无候选的新记忆不进任何批 → 无裁决 → 落库阶段默认 store（无冲突可能，短路到单记忆粒度）
    # 幂等跳过的记忆不进批（已确定 skip，省一次 LLM 调用）
    batched_groups = [g for g in groups if g.candidates and g.new_memory_index not in idem_skip]
    batches = build_batches(batched_groups, budget)
    if not batches:
        if batched_groups:
            # 理论上不可达（batched_groups 非空）；兜底：整批送检（宁超不丢）
            logger.warning("L1.5 build_batches 返回空（防御），退化为单批送检")
            batches = [Batch(
                new_memories=[g.new_memory for g in batched_groups],
                start_index=batched_groups[0].new_memory_index,
                candidates=_merge_group_candidates(batched_groups),
            )]
        else:
            # batched_groups 为空（全部幂等跳过 / 无候选）→ 不送检，落库阶段按 skip 处理
            batches = []

    # 3. 逐批 LLM 裁决（批内 new_memory_index 重映射回全局下标）
    all_decisions: list[ConflictDecision] = []
    for batch in batches:
        pv = PromptStore().get("l1_conflict", bucket_ctx)
        prompt = _render_l15_text(pv.text, batch.new_memories, batch.candidates)
        try:
            text, provider_name, usage = llm_chain.call_with_fallback(
                llm_cfg, prompt, chain_name="refinement", client=client,
            )
        except llm_provider.LLMUnavailable as e:
            result.error = str(e)
            result.anomaly_warn = True
            err_run = RefineRunRecorder.start(
                mem_conn, file_id=bucket_key, stage="l1_conflict",
                version=pv.version, variant=pv.variant,
                provider="unavailable", bucket_key=bucket_key,
            )
            RefineRunRecorder.finish(
                mem_conn, err_run, memories_count=0, action_counts={},
                status="error", error=str(e),
            )
            return result
        try:
            decisions = parse_l15_output(text)
            for d in decisions:
                d.new_memory_index += batch.start_index
            all_decisions.extend(decisions)
            run_status = "ok"
            run_error = None
        except L15Error as e:
            # 2026-08-18（T-53 免费托底）：zhipu 免费档偶发输出截断 → JSON 解析失败。
            # 同模型重试 1 次（提示只输出纯 JSON 数组）——截断是偶发，重试大概率拿完整输出；
            # 2 次仍失败才降级默认 store（保守，不丢数据）。
            logger.warning("L1.5 输出解析失败，重试 1 次: %s", e)
            try:
                retry_prompt = prompt + "\n\n# 注意\n上次输出无法解析为 JSON 数组，请只输出纯 JSON 数组，无其他文字。"
                text, provider_name, usage = llm_chain.call_with_fallback(
                    llm_cfg, retry_prompt, chain_name="refinement", client=client,
                )
                decisions = parse_l15_output(text)
                for d in decisions:
                    d.new_memory_index += batch.start_index
                all_decisions.extend(decisions)
                run_status = "ok"
                run_error = None
                logger.info("L1.5 解析重试成功 provider=%s", provider_name)
            except (L15Error, llm_provider.LLMUnavailable) as e2:
                logger.warning("L1.5 输出解析重试仍失败: %s", e2)
                # 解析失败 → 默认 store（保守，不丢数据）
                decisions = [
                    ConflictDecision(
                        i + batch.start_index, [], "store", reason="L1.5 解析失败默认 store",
                    )
                    for i in range(len(batch.new_memories))
                ]
                all_decisions.extend(decisions)
                run_status = "error"
                run_error = str(e2)
        # 记录 refine_run（每候选批一条；action 分布 + 版本 + provider；memories_count=本批新记忆数）
        action_counts = dict(Counter(d.action for d in decisions))
        run_id = RefineRunRecorder.start(
            mem_conn, file_id=bucket_key, stage="l1_conflict",
            version=pv.version, variant=pv.variant,
            provider=provider_name, bucket_key=bucket_key,
        )
        RefineRunRecorder.finish(
            mem_conn, run_id, memories_count=len(batch.new_memories),
            action_counts=action_counts, status=run_status, error=run_error,
            usage=usage,
        )
        logger.info("L1.5 批完成: version=%s variant=%s provider=%s actions=%s",
                    pv.version, pv.variant, provider_name, action_counts)
        if result.prompt_meta is None:
            result.prompt_meta = {"stage": "l1_conflict", "version": pv.version, "variant": pv.variant}

    # 3.5 T-132：若本批发生预筛降级（embed 不可达 + skip_conflict），独立记一条
    # refine_run 标记，避免与 LLM 判「无变化」(action_counts["skip"]) 同名混淆、事后不可识别
    if prescreen_skipped > 0:
        _record_prescreen_skip_run(mem_conn, bucket_key, prescreen_skipped, bucket_ctx)

    # 4. 四动作落库
    # 同一新记忆只进一批 → 至多一个裁决；按 new_memory_index 聚合（防御跨批重复）
    by_index: dict[int, ConflictDecision] = {}
    for d in all_decisions:
        if d.new_memory_index not in by_index:
            by_index[d.new_memory_index] = d

    for i, new_mem in enumerate(new_memories):
        # 幂等跳过：同源同内容已存在 → 不落库（确定性守卫，覆盖 LLM 漏判 skip 的场景）
        if i in idem_skip:
            result.skipped.append(i)
            continue
        decision = by_index.get(i, ConflictDecision(i, [], "store", reason="无裁决默认 store"))
        action = decision.action
        if action == "store":
            new_id = _store_memory(mem_conn, new_mem, dimensions, source_ref, prompt_version=prompt_version)
            new_mem["memory_id"] = new_id  # 写回，供 L2 场景关联
            result.stored.append(new_id)
        elif action == "skip":
            result.skipped.append(i)
        elif action == "update":
            if decision.candidate_ids:
                new_id = _update_memory(
                    mem_conn, new_mem, decision.candidate_ids[0],
                    dimensions, source_ref, prompt_version=prompt_version,
                )
                new_mem["memory_id"] = new_id
                result.updated.append(new_id)
                result.archived.extend(decision.candidate_ids)
            else:
                # 无候选 → 退化为 store
                new_id = _store_memory(mem_conn, new_mem, dimensions, source_ref, prompt_version=prompt_version)
                new_mem["memory_id"] = new_id
                result.stored.append(new_id)
        elif action == "merge":
            if decision.candidate_ids and decision.merged_content:
                new_id = _merge_memory(
                    mem_conn, new_mem, decision.candidate_ids,
                    decision.merged_content, dimensions, source_ref, prompt_version=prompt_version,
                )
                new_mem["memory_id"] = new_id
                result.merged.append(new_id)
                result.archived.extend(decision.candidate_ids)
            else:
                new_id = _store_memory(mem_conn, new_mem, dimensions, source_ref, prompt_version=prompt_version)
                new_mem["memory_id"] = new_id
                result.stored.append(new_id)

        # T-135：语义边（新记忆落库拿到 memory_id 后写入；skip/无裁决时 new_id 为空 → 0）
        result.semantic_edges_written += _write_semantic_edges(mem_conn, new_mem, decision, cfg)

    logger.info(
        "L1.5 完成: store=%d skip=%d update=%d merge=%d archived=%d warn=%s",
        len(result.stored), len(result.skipped), len(result.updated),
        len(result.merged), len(result.archived), result.anomaly_warn,
    )
    return result
