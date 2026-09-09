"""operations/answer.py：聚合答案与时序推理操作（T-149，B159）。

回答「召回了但用不对」的剪刀差瓶颈（B145 实证：时序 J=0.158 / 跨会话 J=0.227
而 recall 0.78/0.81）——在检索之上增加答案生成层：

- 复用 operations/search.search 取候选（memory scope，含 T-149① 透传的
  occurred_at / facts）
- 题型分派：temporal（时序：时间点定位/先后/间隔）/ aggregate（跨会话计数/
  列举/多跳聚合）/ generic（兜底）——正则启发式分派，入参可显式指定覆盖
- 时序模板注入 occurred_at 升序时间线；聚合模板注入 facts 结构化证据
- LLM 统一走 ``llm.chain.call_with_fallback``（refinement 降级链，B30 零裸调）

分层（v0.7 §7）：本模块不认识协议——不 import fastapi/mcp；LLM 不可用按
「可预期业务失败」返回 ``OperationResult(ok=False, ERR_LLM_UNAVAILABLE)``。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable

from sgme.data.search import search_memories
from sgme.llm import chain as llm_chain
from sgme.llm import provider as llm_provider
from sgme.operations.errors import ERR_LLM_UNAVAILABLE, InvalidArgs, OperationResult
from sgme.prompts.manager import PromptStore

# ---------- 题型分派 ----------

# 时序信号词：间隔/先后/顺序/时间点比较
_TEMPORAL_PAT = re.compile(
    r"(多少天|几天|多久|间隔|先后|顺序|最早|最晚|之前还是|之后还是|先.*还是|哪件.*先|"
    r"how many days|how long|before or after|in what order|first to last)",
    re.I,
)
# 聚合信号词：计数/列举/清单/合计
_AGGREGATE_PAT = re.compile(
    r"(几个|多少个|多少项|多少条|哪些|清单|列表|一共|总计|合计|汇总|列举|"
    r"how many|how much|list all|list every|which .*(items|projects|books))",
    re.I,
)

QUESTION_TYPES = ("temporal", "aggregate", "generic")


def classify_question(query: str) -> str:
    """题型启发式分派：temporal > aggregate > generic（时序信号优先）。"""
    if _TEMPORAL_PAT.search(query or ""):
        return "temporal"
    if _AGGREGATE_PAT.search(query or ""):
        return "aggregate"
    return "generic"


def _stage_for(qtype: str) -> str:
    return {
        "temporal": "answer_temporal",
        "aggregate": "answer_aggregate",
        "generic": "answer_generic",
    }[qtype]


# ---------- 上下文渲染 ----------

def _fmt_ts(ts: str | None) -> str:
    """occurred_at 紧凑展示（空 → 无时间）。"""
    return (ts or "").strip() or "无时间"


def render_context(candidates: list[dict]) -> str:
    """候选 → 编号上下文块（含 facts 与 occurred_at，模板 {{context}}）。"""
    lines: list[str] = []
    for i, r in enumerate(candidates):
        head = f"[{i + 1}] ({_fmt_ts(r.get('occurred_at'))}) {r.get('content', '')}"
        facts = r.get("facts") or []
        if facts:
            fact_strs = [
                f"{f.get('subject', '')}|{f.get('predicate', '')}|{f.get('object', '')}"
                for f in facts
            ]
            head += " 事实: " + "; ".join(fact_strs)
        lines.append(head)
    return "\n".join(lines) or "(no memories retrieved)"


def render_timeline(candidates: list[dict]) -> str:
    """候选 → occurred_at 升序时间线（模板 {{timeline}}）；无时间排最后。"""
    def _key(r: dict) -> tuple[int, str]:
        ts = (r.get("occurred_at") or "").strip()
        return (0 if ts else 1, ts)

    ordered = sorted(candidates, key=_key)
    lines: list[str] = []
    for r in ordered:
        ts = (r.get("occurred_at") or "").strip() or "时间未知"
        lines.append(f"- {ts} :: {r.get('memory_id', '')[:8]} {r.get('content', '')}")
    return "\n".join(lines) or "(empty timeline)"


def build_evidence(candidates: list[dict]) -> list[dict]:
    """候选 → 证据链（memory_id/rank/occurred_at/facts_used）。"""
    out: list[dict] = []
    for r in candidates:
        out.append({
            "memory_id": r.get("memory_id"),
            "rank": r.get("rank"),
            "occurred_at": r.get("occurred_at"),
            "facts_used": r.get("facts") or [],
        })
    return out


# ---------- 主操作 ----------

def answer(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    query: str,
    question_type: str | None = None,
    limit: int = 8,
    client: Any = None,
    llm_fn: Callable[[str], str] | None = None,
) -> OperationResult:
    """聚合答案操作：检索 → 分派题型 → 渲染上下文/时间线 → LLM 生成。

    Args:
        mem_conn / session_conn: memory.db / session.db 连接（search 透传）。
        cfg: 运行时配置（含 llm 段与 answer 段）。
        query: 用户问题。
        question_type: 显式题型覆盖（temporal/aggregate/generic）；None 自动分派。
        limit: 检索候选条数（默认 8 = LongMemEval recall@8 口径）。
        client: 可选 httpx 客户端（测试 mock）。
        llm_fn: 可选 LLM 函数注入（测试 mock）；缺省 call_with_fallback。

    Returns:
        OperationResult.succeed: data = {answer, question_type, evidence,
        provider, usage, prompt_meta, candidates_used}
        OperationResult.fail(ERR_LLM_UNAVAILABLE): 全链 LLM 不可用。
    """
    if not isinstance(query, str) or not query.strip():
        raise InvalidArgs("query 必须为非空字符串")
    qtype = question_type or classify_question(query)
    if qtype not in QUESTION_TYPES:
        raise InvalidArgs(f"question_type 非法: {question_type}（合法 {QUESTION_TYPES}）")

    enabled = (cfg.get("answer") or {}).get("enabled", True)
    if not enabled:
        return OperationResult.fail(
            error_code="ERR_DISABLED",
            message="answer 模块未启用（config.answer.enabled=false）",
        )

    # 检索候选（memory 层；T-149① 起结果带 occurred_at/facts）
    candidates = search_memories(
        mem_conn, session_conn,
        query=query, limit=limit,
        include_sources=False, cfg=cfg,
    )

    pv = PromptStore().get(_stage_for(qtype))
    if qtype == "temporal":
        prompt = (pv.text
                  .replace("{{timeline}}", render_timeline(candidates))
                  .replace("{{context}}", render_context(candidates))
                  .replace("{{question}}", query))
    else:
        prompt = (pv.text
                  .replace("{{context}}", render_context(candidates))
                  .replace("{{question}}", query))

    usage: dict[str, Any] = {}
    provider = "mock" if llm_fn else ""
    if llm_fn is not None:
        text = llm_fn(prompt)
    else:
        try:
            text, provider, usage = llm_chain.call_with_fallback(
                cfg["llm"], prompt, chain_name="refinement", client=client,
            )
        except llm_provider.LLMUnavailable as e:
            return OperationResult.fail(
                error_code=ERR_LLM_UNAVAILABLE,
                message=f"LLM 全链不可用: {e}",
            )

    answer_text = (text or "").strip()
    data = {
        "answer": answer_text or None,
        "question_type": qtype,
        "evidence": build_evidence(candidates),
        "provider": provider,
        "usage": usage,
        "prompt_meta": {"stage": _stage_for(qtype), "version": pv.version},
        "candidates_used": len(candidates),
    }
    return OperationResult.succeed(data)
