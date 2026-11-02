"""sgme/skills/config.py：技能管理模块配置解析（ST-36 v0.2.1 两步门——暂缓建库）。

约定：配置 dict 形如 ``{"skills": {...}}``（即 sgme.config.load_config() 顶层结构），
section 缺失或类型错误时返回全默认配置（enabled=True、source_dirs 空、budget 40）。

索引层为可重建派生物：BM25 内存索引 + 向量缓存文件（data/cache/skill_vectors.json），
无独立数据库（skills.db 两步门：及格线达标则永不建，见设计 §二）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 合法取值
VALID_CACHE_POLICIES = ("lazy", "refresh")

# 缺省值
DEFAULT_ENABLED = True
DEFAULT_BUDGET = 40
DEFAULT_CACHE_POLICY = "lazy"

DEFAULT_SKILLS_CONFIG = {
    "enabled": DEFAULT_ENABLED,
    # 额外扫描的本地 git 工作区目录（SKILL.md 技能树）；空 = 仅 wiki_pages 单源
    "source_dirs": [],
    # L0 常驻列表预算（条数上限，M2 披露端点消费，此处仅承载配置）
    "budget": DEFAULT_BUDGET,
    # 向量缓存策略：lazy（内容 SHA 变化才重嵌）/ refresh（每次全量重嵌，测试用）
    "vector_cache_policy": DEFAULT_CACHE_POLICY,
}


@dataclass
class SkillsConfig:
    """技能管理模块配置（ST-36 设计 §二/§三）。

    Attributes:
        enabled: 模块开关；False 时 init() 返回 None，核心零影响。
        source_dirs: 额外扫描的本地技能目录列表（git 真源工作区）。
        budget: L0 索引常驻条数预算。
        vector_cache_policy: 向量缓存刷新策略（lazy/refresh）。
    """

    enabled: bool = DEFAULT_ENABLED
    source_dirs: list[str] = field(default_factory=list)
    budget: int = DEFAULT_BUDGET
    vector_cache_policy: str = DEFAULT_CACHE_POLICY


def parse_skills_config(cfg: dict | None) -> SkillsConfig:
    """从配置 dict 解析技能管理配置（缺失/类型错误兜底全默认，镜像 skills_hub.config 模式）。"""
    section = (cfg or {}).get("skills")
    if not isinstance(section, dict):
        return SkillsConfig()

    enabled = section.get("enabled", DEFAULT_ENABLED)
    if not isinstance(enabled, bool):
        raise ValueError(f"skills.enabled 必须为 bool（当前: {enabled!r}）")

    source_dirs = section.get("source_dirs", [])
    if not isinstance(source_dirs, list) or not all(isinstance(d, str) for d in source_dirs):
        raise ValueError(f"skills.source_dirs 必须为字符串列表（当前: {source_dirs!r}）")

    budget = section.get("budget", DEFAULT_BUDGET)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError(f"skills.budget 必须为正整数（当前: {budget!r}）")

    policy = str(section.get("vector_cache_policy", DEFAULT_CACHE_POLICY)).lower()
    if policy not in VALID_CACHE_POLICIES:
        raise ValueError(
            f"未知 skills.vector_cache_policy: {policy!r}（可选: {', '.join(VALID_CACHE_POLICIES)}）"
        )

    return SkillsConfig(
        enabled=enabled,
        source_dirs=list(source_dirs),
        budget=budget,
        vector_cache_policy=policy,
    )
