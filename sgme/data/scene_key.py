"""data/scene_key.py：D-53 场景键规则（纯函数，零 LLM）。

规则（设计 §3.0.2，二轮修订定稿）：

    scene_key = <主维度 dimension_id> + ":" + <来源类 origin_class>

- **主维度 R1**：dimension_registry.priority 最小者（身份 10 … 创意 130）；
- **R2 并列兜底**：priority 相同时按 dimension_id 字典序（确定性，禁止随机/插入序）；
- **R3 无标签** → unknown（不猜）。
- **来源类**：agent_tag 映射 4 类（coding / desktop / mobile / other），映射表可配（D-8）：
  coding = codex / claude-code / zcode / mimo / dsh；desktop = hermes / workbuddy；
  mobile = doubao；未登记 agent 或 agent_tag 为空 → other / unknown。
- **时间不进键**：D-48「跨天≥2」是独立条件，时间进键会让「同一场景连续两周」冒充两个场景。
- **副维度不进键**：会把同一生活场景拆碎、虚增场景数。

已知口径（阶段 0 实测，2026-11-04）：agent_tag 覆盖率 82.89%（27,321/32,961），
故 unknown 主要由「无 agent_tag」产生；硬门是「不劣于基线」，不是绝对 0。
"""
from __future__ import annotations

import logging
import os

from sgme import config

logger = logging.getLogger("sgme.data.scene_key")

UNKNOWN = "unknown"


def origin_mapping() -> dict:
    """来源类映射（默认值 + 运行时 sgme.yaml 的 scene 段覆盖；结果缓存）。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    cfg = {
        k: (list(v) if isinstance(v, list) else v)
        for k, v in config.DEFAULT_SCENE_CONFIG.get("origin_class", {}).items()
    }
    default_class = config.DEFAULT_SCENE_CONFIG.get("default_class", "other")
    try:
        user = config.load_sgme_config()
        scene = user.get("scene") if isinstance(user, dict) else None
        if isinstance(scene, dict):
            merged = config._merge_scene_config(scene)  # noqa: SLF001 - 同包合并函数
            cfg = merged.get("origin_class", cfg)
            default_class = merged.get("default_class", default_class)
    except Exception:  # noqa: BLE001 - 配置读取失败回落默认（写路径不能因此失败）
        logger.warning("scene 配置读取失败，回落默认映射", exc_info=True)
    _CACHE = {"origin_class": cfg, "default_class": default_class}
    return _CACHE


_CACHE: dict | None = None


def reset_cache() -> None:
    """测试/热重载用：清空映射缓存。"""
    global _CACHE
    _CACHE = None


def main_dimension(dimension_ids, priorities: dict[str, int | None]) -> str | None:
    """R1/R2/R3：priority 最小者；并列按 id 字典序；空标签 → None。

    Args:
        dimension_ids: 记忆的维度标签（可含重复/空值，函数内部去重）。
        priorities: {dimension_id: priority}；未知维度按 None（最低优先级）。
    """
    ids = sorted({str(d) for d in (dimension_ids or []) if d})
    if not ids:
        return None

    def sort_key(dim: str):
        p = priorities.get(dim)
        # priority 缺失排最后（None 视为最低优先级），并列时字典序
        return (1, 0, dim) if p is None else (0, int(p), dim)

    return min(ids, key=sort_key)


def origin_class(agent_tag: str | None) -> str:
    """agent_tag → 来源类（coding/desktop/mobile/other）。"""
    mapping = origin_mapping()
    default = mapping.get("default_class", "other")
    tag = (agent_tag or "").strip().lower()
    if not tag:
        return default
    for cls, tokens in mapping.get("origin_class", {}).items():
        for token in tokens or []:
            tok = str(token).strip().lower()
            if not tok:
                continue
            if tag == tok or tag.startswith(tok + "-") or tag.startswith(tok + "_"):
                return str(cls)
    return default


def build_scene_key(
    dimension_ids,
    agent_tag: str | None,
    priorities: dict[str, int | None],
) -> str:
    """计算 scene_key；缺主维度或缺 agent_tag → unknown（R3，不猜）。"""
    main = main_dimension(dimension_ids, priorities)
    if main is None or not (agent_tag or "").strip():
        return UNKNOWN
    return f"{main}:{origin_class(agent_tag)}"


def load_priorities(conn) -> dict[str, int | None]:
    """读取维度优先级表（一次查询，供批量回填/单条插入复用）。"""
    try:
        rows = conn.execute("SELECT id, priority FROM dimension_registry").fetchall()
    except Exception:  # noqa: BLE001 - 老库缺列时降级为全 None（不影响落库）
        return {}
    return {str(r[0]): (None if r[1] is None else int(r[1])) for r in rows}
