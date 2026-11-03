# -*- coding: utf-8 -*-
"""tests/test_wiki_classify.py：知识库 2.0 分类/页型/标题行规则（D-62 / §5.1）。

覆盖：路径前缀映射（research/skill/design/log/entity）、已是类名幂等、
标题/正文特征兜底、未分类回退、页型映射、标题行取首行且截断。
"""
from __future__ import annotations

from sgme import wiki_classify as wc


def test_prefix_rules_map_to_seven_categories():
    assert wc.classify_category("research/dsh") == "研究调研"
    assert wc.classify_category("skill/sgme") == "操作手册"
    assert wc.classify_category("design/sgme") == "设计文档"
    assert wc.classify_category("log/2026-10") == "运行日志"
    assert wc.classify_category("entity/leo") == "实体档案"


def test_existing_category_name_is_idempotent():
    for name in wc.CATEGORY_NAMES:
        assert wc.classify_category(name, "任意标题", "正文") == name


def test_text_rules_fallback_when_category_missing():
    assert wc.classify_category(None, "索引：所有手册", "") == "索引"
    assert wc.classify_category(None, "2026-10 巡检记录", "") == "运行日志"
    assert wc.classify_category(None, "如何部署 NAS", "") == "操作手册"
    assert wc.classify_category(None, "SGME 架构设计决策", "") == "设计文档"


def test_unknown_falls_back_to_unclassified():
    assert wc.classify_category("random-cat", "天气", "今天下雨") == wc.UNCLASSIFIED
    assert wc.classify_category(None, "", "") == wc.UNCLASSIFIED


def test_page_type_mapping():
    assert wc.page_type_for("运行日志") == "log"
    assert wc.page_type_for("实体档案") == "entity"
    assert wc.page_type_for("索引") == "index"
    assert wc.page_type_for("研究调研") == "topic"
    assert wc.page_type_for("操作手册") == "topic"


def test_headline_first_line_and_fallback():
    assert wc.headline("# 结论\n正文") == "结论"
    assert wc.headline("> 引用行\n正文") == "引用行"
    assert wc.headline("", "回退标题") == "回退标题"
    long_line = "一" * 50
    assert len(wc.headline(long_line, "")) == 30


def test_postprocess_bundles_three_outputs():
    out = wc.postprocess("research/dsh", "研究 DSH 插件", "# 结论\n正文")
    assert out["category"] == "研究调研"
    assert out["page_type"] == "topic"
    assert out["headline"] == "结论"
