"""T-149② 测试：answer 三模板注册与占位符。

覆盖：
- PromptStore.get 三 stage 可读（working 副本，version=working-sha）
- 必备占位符存在
- manifest stages 注册（未知 stage 报错语义不变）
"""

from __future__ import annotations

import pytest

from sgme.prompts.manager import PromptStore


@pytest.mark.parametrize("stage,required", [
    ("answer_aggregate", ["{{context}}", "{{question}}"]),
    ("answer_temporal", ["{{context}}", "{{question}}", "{{timeline}}"]),
    ("answer_generic", ["{{context}}", "{{question}}"]),
])
def test_answer_prompts_registered(stage, required):
    pv = PromptStore().get(stage)
    assert pv.text, f"{stage} 工作副本为空"
    for p in required:
        assert p in pv.text, f"{stage} 缺占位符 {p}"


def test_unknown_stage_still_errors():
    with pytest.raises(Exception):
        PromptStore().get("answer_nonexistent")


@pytest.mark.parametrize("stage", ["answer_generic", "answer_aggregate", "answer_temporal"])
def test_answer_prompts_product_mode_not_overstrict(stage):
    """产品档：允许综合记忆作答；NO CONTEXT 仅在证据明显不足时。"""
    pv = PromptStore().get(stage)
    assert "NO CONTEXT" in pv.text
    # 不得再是「只使用…如果记忆不包含答案就 NO CONTEXT」的评测拒答口径
    assert "只使用上面检索到的记忆内容作答" not in pv.text
    assert "综合" in pv.text or "综合所有" in pv.text or "可综合" in pv.text
