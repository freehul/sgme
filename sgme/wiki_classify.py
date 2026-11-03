"""wiki_classify.py：知识库 2.0 分类与页型规则（零 LLM，D-62 / §5.1）。

设计口径（§5.1 实施状态 / D-62）：
- 分类注册表 `wiki_categories` ≤10 个一级类，默认 7 类：研究调研 / 操作手册 / 设计文档 /
  运行日志 / 实体档案 / 索引 / 未分类；
- 自动分类 = 规则预判兜底（来源路径 + 文档类型特征），LLM 顺带分类属 L2 链路后续接线；
- 未命中回退「未分类」并**告警**（未分类占比 >5% 出清单，D-47 同款：不许静默）；
- 页型 `page_type` 四值：entity / topic / log / index（与分类正交：分类是「讲什么」，
  页型是「怎么组织」）。

本模块只做纯规则，不读写库、不调 LLM —— DAO 与回填脚本共用同一套判定。
"""
from __future__ import annotations

import re

# 默认 7 类（name, display_name, sort_order）
DEFAULT_CATEGORIES: tuple[tuple[str, str, int], ...] = (
    ("研究调研", "研究调研", 10),
    ("操作手册", "操作手册", 20),
    ("设计文档", "设计文档", 30),
    ("运行日志", "运行日志", 40),
    ("实体档案", "实体档案", 50),
    ("索引", "索引", 60),
    ("未分类", "未分类", 99),
)

CATEGORY_NAMES: tuple[str, ...] = tuple(c[0] for c in DEFAULT_CATEGORIES)
UNCLASSIFIED = "未分类"

# 一级路径（旧 category 的 "/" 前段）→ 类名。skill/* 属 306 页技能手册类内容，
# 4.7 划界后应归技能库；在划界完成前先归「操作手册」，不丢页。
_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    ("research", "研究调研"),
    ("skill", "操作手册"),
    ("design", "设计文档"),
    ("log", "运行日志"),
    ("entity", "实体档案"),
    ("index", "索引"),
    ("ops", "运行日志"),
    ("runbook", "操作手册"),
)

# 标题/正文特征兜底（顺序即优先级）
_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"索引|目录|index", re.I), "索引"),
    (re.compile(r"运行日志|日志|巡检记录|incident|logbook", re.I), "运行日志"),
    (re.compile(r"操作手册|手册|指南|教程|步骤|如何|怎么|how[-\s]?to|runbook", re.I), "操作手册"),
    (re.compile(r"设计文档|设计方案|架构|设计决策|方案 v\d|ADR", re.I), "设计文档"),
    (re.compile(r"调研|研究|竞品|测评|调查", re.I), "研究调研"),
    (re.compile(r"实体档案|人物|角色档案|成员档案", re.I), "实体档案"),
)

_INDEX_HINTS = re.compile(r"索引|目录|index", re.I)


def classify_category(raw_category: str | None, title: str = "", content: str = "") -> str:
    """规则分类：已是合法类名 → 原样；路径前缀 → 类名；标题/正文特征兜底；否则未分类。"""
    raw = (raw_category or "").strip()
    if raw in CATEGORY_NAMES:
        return raw
    if raw:
        head = raw.split("/", 1)[0].strip().lower()
        for prefix, cls in _PREFIX_RULES:
            if head == prefix:
                return cls
        # 中文一级路径（如「研究/DSH」）
        for cls in CATEGORY_NAMES:
            if raw.startswith(cls):
                return cls
    probe = f"{title}\n{content[:200]}"
    for pattern, cls in _TEXT_RULES:
        if pattern.search(probe):
            return cls
    return UNCLASSIFIED


def page_type_for(category: str, title: str = "") -> str:
    """页型：运行日志 → log；实体档案 → entity；索引 → index；其余 topic。"""
    if category == "运行日志":
        return "log"
    if category == "实体档案":
        return "entity"
    if category == "索引" or _INDEX_HINTS.search(title or ""):
        return "index"
    return "topic"


_MD_PREFIX = re.compile(r"^[#>\-\*\s]+")


def headline(content: str | None, title: str | None = None, max_len: int = 30) -> str:
    """标题行：取正文第一行去掉 Markdown 前缀，≤max_len；空则确定性回退到 title。"""
    for line in (content or "").splitlines():
        text = _MD_PREFIX.sub("", line).strip()
        if text:
            return text[:max_len]
    return (title or "").strip()[:max_len]


def postprocess(raw_category: str | None, title: str, content: str) -> dict:
    """一次算出三件：分类 / 页型 / 标题行（DAO 写入与回填共用）。"""
    category = classify_category(raw_category, title, content)
    return {
        "category": category,
        "page_type": page_type_for(category, title),
        "headline": headline(content, title),
    }
