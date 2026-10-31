"""沉淀清单抽取（零 LLM）测试。

背景：用户否定「一个记忆过两次 LLM」的方案（2026-10-31）。会话原件里本来就留有
沉淀动作的痕迹，正则抽取即可，零 token；抽出来的清单用于两件事：
1 进 L1 提示词，让模型对已沉淀主题只写摘要不写细节
2 落库时按标题重合度建「记忆 → 知识页/技能」关联
"""

from __future__ import annotations

from sgme.engine import sinking


def test_extracts_wiki_page_from_tool_result():
    text = (
        "调用 sgme_wiki_page_add：{\"title\": \"GLM 试用桶参数口径\", \"content\": \"...\"}\n"
        "返回：{\"page_id\": \"glm-试用桶当提炼模型的参数口径-实测-4024c754\", \"status\": \"created\"}"
    )
    refs = sinking.extract_sunk_refs(text)
    assert len(refs) == 1
    assert refs[0].kind == "wiki"
    assert refs[0].ref_id == "glm-试用桶当提炼模型的参数口径-实测-4024c754"
    assert refs[0].title == "GLM 试用桶参数口径"


def test_extracts_skill_from_put_call():
    text = "sgme_skill_put: {\"name\": \"sgme-operations\", \"content\": \"...\"}\n返回 ok"
    refs = sinking.extract_sunk_refs(text)
    assert [(r.kind, r.ref_id) for r in refs] == [("skill", "sgme-operations")]


def test_extracts_skill_from_path():
    text = "-- 手册在 D:\\HermesAgent\\skills\\ai\\secrets-handling\\SKILL.md"
    refs = sinking.extract_sunk_refs(text)
    assert [(r.kind, r.ref_id) for r in refs] == [("skill", "secrets-handling")]


def test_dedupes_and_caps():
    text = "\n".join(
        f"skill_put: {{\"name\": \"skill-{i % 3}\"}}" for i in range(40)
    )
    refs = sinking.extract_sunk_refs(text, max_refs=5)
    assert len(refs) <= 5
    assert len({(r.kind, r.ref_id) for r in refs}) == len(refs), "不得重复"


def test_empty_and_noise_return_empty():
    assert sinking.extract_sunk_refs("") == []
    assert sinking.extract_sunk_refs("用户今天心情不错，聊了聊剧本。") == []


def test_render_marks_empty_explicitly():
    assert "未发现" in sinking.render_sunk_refs([])
    text = sinking.render_sunk_refs([
        sinking.SunkRef("wiki", "page-1", "参数口径手册"),
        sinking.SunkRef("skill", "sgme-operations"),
    ])
    assert "知识页《参数口径手册》" in text
    assert "技能 `sgme-operations`" in text


def test_link_targets_matches_by_title_overlap():
    refs = [
        sinking.SunkRef("wiki", "page-1", "GLM 参数口径"),
        sinking.SunkRef("wiki", "page-2", "完全无关的主题"),
    ]
    content = "给 GLM 配提炼参数时要用 reasoning_effort=low，参数口径见手册"
    hits = sinking.link_targets(content, refs)
    assert [h.ref_id for h in hits] == ["page-1"], "只挂标题相关的，不乱挂"


def test_link_targets_ignores_too_short_title():
    refs = [sinking.SunkRef("wiki", "page-1", "参数")]
    assert sinking.link_targets("参数", refs) == []
