# -*- coding: utf-8 -*-
"""engine/stage_map.py：2.0 分层口径 ↔ 代码 stage 名的唯一映射。

背景（2026-11-05 用户定）：2.0 D-5 的分层编号是 L1 捕获 → L2 提取 → L3 裁决 → L4 梦境
（废弃 1.x 的 L0 / L1.5 等编号），但代码/数据库里沿用了旧名，导致汇报、看板、排障时
反复对错层（实例：把 L2 提取的 token 说成 L1、把 L3 裁决说成 L1.5、把遗留场景聚合说成 L2）。

本模块是唯一口径源：
- 写入侧保留原始 stage 键（历史 7 万+ 行不动，兼容既有查询/门禁）；
- 展示侧（API 附加字段、日志、日报、WebUI）一律经 stage_label() 转成 2.0 名称；
- 新代码注释/文档提到阶段时，也使用 2.0 名称 + 本表对照。

口径表（2026-11-05 定稿）：

| 2.0 层 | 名称 | 代码 stage | 状态 |
|---|---|---|---|
| L1 | 捕获/剪枝 | （无 refine_run；raw 写入 + prune.py） | 零 LLM |
| L2 | 提取 | l1_extraction | 活跃（旧名技术债） |
| L3 | 裁决 | l1_conflict | 活跃（旧名技术债） |
| L4 | 梦境 | dream（守护定时器，零 LLM） | 活跃 |
| — | 遗留场景聚合 | l2_scene | 1.x 遗留，D-53 已取消，9.6 下线范围 |
| — | 历史画像摘要 | tier0_summary | D-79 已砍（2026-11-05），**仅历史展示** |

禁止再使用：L0 / L1.5 / L2 场景（指旧聚合层）作为阶段名。
"""

from __future__ import annotations

#: 代码 stage 键 → (2.0 层号, 中文名, 英文键)
STAGE_CANONICAL: dict[str, tuple[str, str, str]] = {
    "l1_extraction": ("L2", "提取", "L2_extract"),
    "l1_conflict": ("L3", "裁决", "L3_adjudicate"),
    "l2_scene": ("—", "遗留场景聚合", "legacy_scene"),
    "dream": ("L4", "梦境", "L4_dream"),
    # D-79（2026-11-05）：Tier0 画像摘要已整体砍除，写侧不再产生该 stage；
    # 生产 refine_runs 仍有 2 万+ 条历史行，保留映射**仅用于历史展示/过滤**：
    # 删掉会让 Dashboard 与 /v1/admin/refine_runs 把历史行退化成原始键 "tier0_summary"。
    "tier0_summary": ("—", "用户画像", "legacy_tier0"),
}

#: 允许作为 stage 查询参数的原始键（与 refine 契约兼容）
KNOWN_STAGE_KEYS: tuple[str, ...] = tuple(STAGE_CANONICAL.keys())

#: 2.0 规范英文键 → 原始 stage 键（反向查询用）
STAGE_KEY_BY_CANONICAL: dict[str, str] = {v[2]: k for k, v in STAGE_CANONICAL.items()}


def canonical_stage(stage: str | None) -> str:
    """原始 stage 键 → 规范英文键；未知 stage 原样返回（前向兼容）。"""
    if not stage:
        return ""
    return STAGE_CANONICAL.get(str(stage), (None, None, str(stage)))[2]


def stage_label(stage: str | None) -> str:
    """原始 stage 键 → 展示名（如 "L2 提取"）；未知 stage 原样返回。

    - 空值 → ""
    - 未知键 → 原键（不猜、不吞，便于发现新增阶段）
    """
    if not stage:
        return ""
    entry = STAGE_CANONICAL.get(str(stage))
    if entry is None:
        return str(stage)
    layer, zh, _en = entry
    return f"{layer} {zh}" if layer and layer != "—" else zh


def stage_key_of(canonical: str) -> str | None:
    """规范英文键（如 "L3_adjudicate"）→ 原始 stage 键；未知返回 None。"""
    return STAGE_KEY_BY_CANONICAL.get(str(canonical))
