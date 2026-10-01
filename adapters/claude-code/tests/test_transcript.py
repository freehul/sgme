# -*- coding: utf-8 -*-
"""transcript 解析：只提取真实对话文本，过滤工具噪声 / 旁支 / 元消息。"""
from claude_sgme.transcript import extract_messages, extract_text

TRANSCRIPT_LINES = [
    '{"type":"mode","mode":"normal","sessionId":"s"}',
    '{"type":"summary","summary":"older stuff"}',
    '{"type":"user","message":{"role":"user","content":"你好"},"uuid":"u1","timestamp":"t1"}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"thinking","thinking":"..."},{"type":"text","text":"回复A"}]},"uuid":"a1","timestamp":"t2"}',
    '{"type":"user","toolUseResult":{},"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"x","content":[{"type":"text","text":"tool output"}]}]},"uuid":"u2"}',
    '{"type":"user","isMeta":true,"message":{"role":"user","content":"caveat"},"uuid":"u3"}',
    '{"type":"assistant","isSidechain":true,"message":{"role":"assistant","content":[{"type":"text","text":"sidechain"}]},"uuid":"a2"}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t","name":"Bash","input":{}}]},"uuid":"a3"}',
    '{"type":"system","subtype":"local_command","content":"x"}',
    "not json at all",
    "",
]


def test_extract_text_variants():
    assert extract_text("hello") == "hello"
    assert (
        extract_text(
            [
                {"type": "text", "text": "a"},
                {"type": "thinking", "thinking": "x"},
                {"type": "text", "text": "b"},
            ]
        )
        == "a\n\nb"
    )
    # tool_result 内部的 text 不得下钻
    assert extract_text([{"type": "tool_result", "content": [{"type": "text", "text": "nested"}]}]) == ""
    assert extract_text(None) == ""


def test_extract_messages_filters_meta_and_tool_noise():
    messages = extract_messages(TRANSCRIPT_LINES)
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "你好"),
        ("assistant", "回复A"),
    ]
    assert messages[0]["uuid"] == "u1"
    assert messages[1]["timestamp"] == "t2"


def test_unknown_types_are_tolerated():
    assert extract_messages(['{"type":"future_thing","payload":1}']) == []
