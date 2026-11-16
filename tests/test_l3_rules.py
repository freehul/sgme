"""L3 规则裁决测试（D-28 / D-29 / D-31）。

规则层要能独立消化三类：完全重复 → skip、明确改口 → supersede、无关 → store；
其余交 LLM。灰区宁可多花 token，不漏改口。
"""

from __future__ import annotations

import pytest

from sgme.engine.l3_rules import (
    adjudicate,
    has_change_signal,
    normalize_text,
)


def _mem(mid, content, subject="self", dims=("preferences",)):
    return {"memory_id": mid, "content": content, "subject": subject, "dimensions": list(dims)}


def _new(content, subject="self", dims=("preferences",)):
    return {"content": content, "subject": subject, "dimensions": list(dims)}


def test_normalize_strips_punct_and_width():
    assert normalize_text("用户要求：删除操作一律走回收站。") == normalize_text(
        "用户要求 删除操作一律走回收站"
    )


@pytest.mark.parametrize("text", [
    "用户不再使用 Notion 了", "现在改成 Obsidian", "no longer using Notion",
    "switched to Obsidian", "以前是用 Evernote 的",
])
def test_change_signals_detected(text):
    assert has_change_signal(text) is True


@pytest.mark.parametrize("text", [
    "用户偏好傍晚散步", "删除操作一律走回收站", "用户住在小城",
])
def test_no_false_change_signal(text):
    assert has_change_signal(text) is False


def test_exact_duplicate_is_skipped():
    old = _mem("m1", "用户要求删除操作一律走回收站")
    d = adjudicate(_new("用户要求：删除操作一律走回收站。"), [old])
    assert d.kind == "skip"
    assert d.target_id == "m1"


def test_same_text_different_subject_is_not_duplicate():
    old = _mem("m1", "每天早上六点起床", subject="others:James")
    d = adjudicate(_new("每天早上六点起床"), [old])
    assert d.kind != "skip"


def test_change_signal_supersedes_same_dimension():
    old = _mem("m1", "用户偏好用 Notion 记笔记")
    d = adjudicate(_new("用户不再用 Notion 记笔记，现在改成 Obsidian"), [old])
    assert d.kind == "supersede"
    assert d.target_id == "m1"


def test_change_signal_without_dim_overlap_goes_to_llm_or_store():
    old = _mem("m1", "用户偏好用 Notion 记笔记", dims=("tech_stack",))
    d = adjudicate(_new("用户不再用 Notion 记笔记"), [old])
    assert d.kind in ("store", "llm")


def test_high_similarity_without_signal_goes_to_llm():
    old = _mem("m1", "用户每周三去图书馆借书")
    d = adjudicate(_new("用户每周三去图书馆借书和还书"), [old])
    assert d.kind == "llm"


def test_unrelated_memory_stores():
    old = _mem("m1", "用户偏好傍晚散步")
    d = adjudicate(_new("用户的笔记本显存是 8GB"), [old], overlap_gray=0.6)
    assert d.kind == "store"


def test_no_candidates_stores():
    d = adjudicate(_new("用户喜欢喝不加糖的茶"), [])
    assert d.kind == "store"


def test_empty_new_content_stores():
    """空正文不在这里拦（交给入库校验），避免规则层与解析层职责重叠。"""
    d = adjudicate({"content": "", "subject": "self", "dimensions": []}, [])
    assert d.kind == "store"


# ---------- T-316：改口词表注册表化 ----------

def test_change_signals_loaded_from_registry():
    """词表须从 resources/registry/change_signals.yaml 加载（D-28 版本化要求）。"""
    from sgme.engine import l3_rules

    assert "不再" in l3_rules.CHANGE_SIGNALS_ZH_LOADED
    assert "no longer" in l3_rules.CHANGE_SIGNALS_EN_LOADED
    # 加载函数对缺失/损坏文件回退内置兜底（主链不被注册表问题挡住）
    zh_fb, en_fb = l3_rules._load_change_signals()
    assert zh_fb and en_fb


def test_change_signal_from_registry_content():
    assert has_change_signal("现在改成 postgres 了") is True
    assert has_change_signal("用户偏好全程中文沟通") is False
