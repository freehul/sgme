"""L3 裁决·规则层（SGME 2.0 D-28 / D-29 / D-31，脚手架版）。

设计原文要点：

- **D-28 Supersession 严格化**：三关 = 判重（归一化 merge 计数）→ 改口检测
  （显式改口信号 + 中英否定词表）+ 同主体对齐（维度 + 主体名粗对齐）→ 灰区转 LLM。
  动作：旧记忆打「被取代」标记 + 归档链 + `superseded_by` 指向新记忆，**永不物理删除**。
- **D-29 合并语义**：精确归一 → 语义 top-5 阈值探测 → LLM 1v1 倾向 UPDATE 不 CREATE。
- **D-31 双分数不加**：L3 只判「与存量关系」（重复/改口），不做重要性自评。

本模块只做**纯规则判定**，不碰数据库、不调 LLM：输入「新记忆 + 候选集」，
输出 `RuleDecision`。灰区一律交回调用方走 LLM（宁可多花一点 token，不漏改口）。

定位（与 1.x L1.5 的关系）：1.x 的 L1.5 每次调用要带近 10 万 token 的候选池
（占全月输入 80%）。本层先用规则消化「明显重复 / 明显改口 / 明显无关」三类，
只有灰区才付 LLM 成本——这是 2.0 砍掉 80% 裁决成本的主要落点。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

# 决策类型：
#   store    → 直接入库（与存量无关）
#   skip     → 丢弃（与存量完全重复）
#   supersede → 取代老记忆（明确改口）
#   llm      → 灰区，交 LLM 1v1 裁决
DecisionKind = Literal["store", "skip", "supersede", "llm"]

# 改口信号词表（D-28：中英双语；扩口语改句式时只改这里，版本化进注册表）
CHANGE_SIGNALS_ZH = (
    "不再", "改成", "换成", "改回", "改用", "现在用", "现在是", "现在改",
    "其实早就", "以前是", "已经不用", "不做了", "算了不改了", "停止使用",
)
CHANGE_SIGNALS_EN = (
    "no longer", "instead", "switched to", "now using", "used to", "stopped using",
    "changed to", "not anymore", "anymore", "replaced with",
)

# 判重前的归一化：去标点/空白/大小写差异，中文全角转半角
_PUNCT_RE = re.compile(r"[\s，。！？、；：\"'（）()\[\]{}<>《》!?,.;:]+")


@dataclass(frozen=True)
class RuleDecision:
    """规则层裁决结果。

    Attributes:
        kind: store / skip / supersede / llm
        target_id: skip/supersede 时指向的候选记忆 id；llm 时指向最可能的候选
        reason: 判定依据（写审计，便于归因）
        confidence: 0–1，规则置信度（低于调用方阈值时也应转 llm）
    """

    kind: DecisionKind
    target_id: str | None = None
    reason: str = ""
    confidence: float = 0.0


def normalize_text(text: str) -> str:
    """归一化文本用于判重：全角转半角、去标点空白、小写。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _PUNCT_RE.sub("", s)
    return s.lower()


def has_change_signal(text: str) -> bool:
    """是否含明确改口信号（D-28 第二关）。"""
    low = str(text or "").lower()
    return any(sig in low for sig in CHANGE_SIGNALS_ZH) or any(
        sig in low for sig in CHANGE_SIGNALS_EN
    )


def _subject_of(mem: dict[str, Any]) -> str:
    return str(mem.get("subject") or "unknown").strip().lower()


def _dims_of(mem: dict[str, Any]) -> set[str]:
    dims = mem.get("dimensions") or []
    if isinstance(dims, dict):  # 2.0 {primary, secondary}
        out = {dims.get("primary")} if dims.get("primary") else set()
        out.update(dims.get("secondary") or [])
        return {str(d) for d in out if d}
    return {str(d) for d in dims}


def _char_overlap(a: str, b: str) -> float:
    """字符二元组 Jaccard——不引外部依赖的粗相似度（语义比对仍交 LLM）。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    inter = len(ga & gb)
    union = len(ga | gb)
    return inter / union if union else 0.0


def adjudicate(
    new_memory: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    overlap_gray: float = 0.30,
    overlap_same: float = 0.85,
) -> RuleDecision:
    """对一条新记忆给规则层裁决。

    Args:
        new_memory: 形如 ``{"content", "subject", "dimensions"}``
        candidates: 候选存量记忆（同主体优先），每条形如
            ``{"memory_id", "content", "subject", "dimensions"}``
        overlap_gray: 相似度高于此值即视为灰区（交 LLM）
        overlap_same: 高于此值且无改口信号 → 判为重复

    Returns:
        RuleDecision。

    判定顺序（与 D-28 三关一致）：
        1. 完全重复（归一化后文本相同 + 同主体）→ skip
        2. 明确改口（同主体 + 同维度有交集 + 含改口信号）→ supersede
        3. 高相似但无改口信号 → skip 偏保守？不：判 llm（可能是补充细节）
        4. 中等相似 → llm
        5. 其余 → store
    """
    new_text = normalize_text(new_memory.get("content"))
    new_subject = _subject_of(new_memory)
    new_dims = _dims_of(new_memory)
    if not new_text:
        return RuleDecision("store", reason="新记忆正文为空，直接走入库校验")

    best: tuple[float, dict[str, Any]] | None = None
    for cand in candidates:
        cand_text = normalize_text(cand.get("content"))
        if not cand_text:
            continue
        same_subject = _subject_of(cand) == new_subject
        # 第 1 关：完全重复
        if same_subject and cand_text == new_text:
            return RuleDecision(
                "skip", str(cand.get("memory_id")),
                f"归一化文本完全相同且主体一致（{new_subject}）", 0.95,
            )
        sim = _char_overlap(new_text, cand_text)
        if best is None or sim > best[0]:
            best = (sim, cand)

    if best is None:
        return RuleDecision("store", reason="无候选记忆，直接入库", confidence=0.9)

    sim, cand = best
    cand_subject = _subject_of(cand)
    dim_overlap = bool(new_dims & _dims_of(cand))
    same_subject = cand_subject == new_subject

    # 第 2 关：明确改口 → 取代
    if same_subject and dim_overlap and has_change_signal(new_memory.get("content")):
        return RuleDecision(
            "supersede", str(cand.get("memory_id")),
            f"同主体同维度且含改口信号（相似度 {sim:.2f}）", 0.85,
        )

    # 第 3 关：灰区 → LLM
    if sim >= overlap_same:
        return RuleDecision(
            "llm", str(cand.get("memory_id")),
            f"高度相似（{sim:.2f}）但无改口信号，需 1v1 判定是补充还是重复", 0.5,
        )
    if sim >= overlap_gray:
        return RuleDecision(
            "llm", str(cand.get("memory_id")),
            f"中等相似（{sim:.2f}）且维度{'有' if dim_overlap else '无'}交集，交 LLM", 0.4,
        )
    return RuleDecision("store", reason=f"与最近候选相似度低（{sim:.2f}），直接入库", confidence=0.8)
