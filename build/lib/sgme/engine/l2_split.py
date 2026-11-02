"""L2 提取·拆条规则（SGME 2.0 D-21 / D-22）。

设计原文（`SGME2.0-设计方案-V1.0.md` §3.2）：

- **D-21 主干+侧面模型**：默认整存一条主干（多维标签）；侧面升级为独立记忆的判据是
  三问——**生命周期不同 / 独立检索 / 独立溯源**。三问**规则化写死在代码，LLM 不做裁决**。
- **D-22 三问规则表**：约束/偏好 → 必拆（长 TTL）；状态/感受 → 必不拆（留主干字段）；
  事件（带独立时间锚）→ 拆为事件；时间缺失 → 不拆兜底。

本模块只做判定，不做拆条与落库——调用方（解析层）按 `SplitDecision` 决定
「并入主干」还是「另立一条记忆」，因果边由 `causes` 另行处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# facts.type 七值（D-66）：约束 / 习惯 / 偏好 / 状态 / 事实 / 经验 / 想法
LONG_LIVED_TYPES = frozenset({"约束", "偏好"})
SHORT_LIVED_TYPES = frozenset({"状态"})
# 经验按 D-23 分流技能库，不进记忆池
SKILL_ROUTE_TYPES = frozenset({"经验"})


@dataclass(frozen=True)
class SplitDecision:
    """拆条判定结果。

    Attributes:
        split: True = 另立一条独立记忆；False = 并入主干。
        reason: 判定依据（写进日志/审计，便于事后归因）。
        route: 需要改写的路由（如经验 → skill_candidate）；None = 沿用主干 route。
    """

    split: bool
    reason: str
    route: str | None = None


def decide_split(fact: dict[str, Any], main_event: dict[str, Any] | None = None) -> SplitDecision:
    """按 D-22 规则表判定一条事实是否升级为独立记忆。

    Args:
        fact: 形如 ``{"type": "偏好", "content": "...", "occurred_at": "..."}``
        main_event: 主干（``{"content", "occurred_at", "feeling"}``），用于比对时间锚。

    Returns:
        SplitDecision。未知/缺失 type 一律按「不拆」兜底——宁可少拆，不可错拆
        （错拆会让主干语义空洞化，且难以后并）。
    """
    ftype = str(fact.get("type") or "").strip()
    if ftype in LONG_LIVED_TYPES:
        return SplitDecision(True, f"{ftype}·生命周期长于主干事件（D-22 必拆）")
    if ftype in SKILL_ROUTE_TYPES:
        return SplitDecision(True, "经验类分流技能库，不进记忆池（D-23）", route="skill_candidate")
    if ftype in SHORT_LIVED_TYPES:
        return SplitDecision(False, "状态留主干字段，短周期不独立成记忆（D-22 必不拆）")
    if ftype == "习惯":
        # D-22 未列习惯（D-66 新增七值后才存在）。习惯与偏好同属长期属性 → 按同族拆。
        return SplitDecision(True, "习惯属长期属性，同族于偏好（D-22 未列，🔶待确认）")
    if ftype == "事实":
        own_anchor = str(fact.get("occurred_at") or "").strip()
        main_anchor = str((main_event or {}).get("occurred_at") or "").strip()
        if own_anchor and own_anchor != main_anchor:
            return SplitDecision(True, "事实带独立时间锚 → 按事件拆（D-22）")
        return SplitDecision(False, "事实无独立时间锚 → 不拆兜底（D-22）")
    # 想法 / 未知类型：讨论中或未定，留主干，不独立成记忆
    return SplitDecision(False, f"类型 {ftype or '缺失'} 不满足三问 → 不拆兜底")
