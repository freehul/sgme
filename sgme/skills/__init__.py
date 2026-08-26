"""sgme/skills：技能管理模块（ST-36 v0.2.1，`skills.enabled` 控制）。

定位（设计 §一/§二）：吸收/调用/回写/新增四闭环的 SGME 侧实现——
git 真源 + 可重建派生索引（两步门：BM25 内存索引 + 向量可弃缓存，暂缓建库 skills.db）。
与旧 sgme/skills_hub（工作区+git同步）分立不混居，防巨无霸回归；git 同步层按需复用旧模块。

用法::

    from sgme.skills import init, index_all
    hub = init(cfg)            # cfg = load_config() 顶层 dict；禁用返回 None
    records = index_all(hub.config.source_dirs if hub else [], wiki_conn)
"""
from __future__ import annotations

from sgme.skills.config import (
    DEFAULT_SKILLS_CONFIG,
    SkillsConfig,
    parse_skills_config,
)
from sgme.skills.indexer import SkillRecord, index_all, validate_name

__all__ = [
    "DEFAULT_SKILLS_CONFIG",
    "SkillRecord",
    "SkillsConfig",
    "index_all",
    "init",
    "parse_skills_config",
    "validate_name",
]


def init(cfg: dict | None) -> SkillsConfig | None:
    """解析配置并返回 SkillsConfig；enabled=false 返回 None（镜像 skills_hub.init 模式）。"""
    config = parse_skills_config(cfg)
    return config if config.enabled else None
