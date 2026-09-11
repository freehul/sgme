"""L1 提炼语言守门测试（2026-09-12 LongMemEval 诊断驱动）。

背景：本地 9B 会把英文会话译成中文记忆（实测一半记忆如此），而英文语料的问题用
英文提问 → BM25 完全失配、向量跨语言更弱 → 检索命中率腰斩。守门规则：**会话以
英文为主、而提炼出的记忆以中文为主** → 判定为语言漂移，带语言提示重试一次。
（中文会话被提炼成中文记忆属正常，不得误报。）
"""

from __future__ import annotations

from sgme.engine.l1 import _lang_mismatch

EN_CONV = ("user: I have been collecting vintage cameras for a few months now.\n"
           "assistant: That is a lovely hobby; which models do you own?\n") * 12
ZH_CONV = ("用户：我最近在收集复古相机，已经有三四个月了。\n"
           "助手：这是个很好的爱好，你现在都有哪些型号？\n") * 12


def test_flags_english_conversation_stored_as_chinese():
    memories = [{"content": "用户已经收集复古相机三个月了。"},
                {"content": "用户拥有 17 台复古相机。"}]
    assert _lang_mismatch(EN_CONV, memories) is True


def test_no_flag_when_memories_keep_english():
    memories = [{"content": "User has been collecting vintage cameras for three months."},
                {"content": "User owns 17 vintage cameras."}]
    assert _lang_mismatch(EN_CONV, memories) is False


def test_no_flag_for_chinese_conversation():
    memories = [{"content": "用户已经收集复古相机三个月了。"}]
    assert _lang_mismatch(ZH_CONV, memories) is False


def test_no_flag_for_empty_or_short_inputs():
    assert _lang_mismatch(EN_CONV, []) is False
    assert _lang_mismatch(EN_CONV, [{"content": ""}]) is False
    # 源会话太短（不足以下语言结论）时不判
    assert _lang_mismatch("hello there", [{"content": "用户住上海。"}]) is False


def test_mixed_memories_not_flagged_when_latin_dominates():
    memories = [{"content": "User prefers Sony-compatible accessories."},
                {"content": "user is looking for camera flash options compatible with Sony A7R IV."},
                {"content": "用户拥有 A7R IV。"}]
    assert _lang_mismatch(EN_CONV, memories) is False


def test_drift_triggers_retry_with_language_hint():
    """英文会话 + 首轮返回中文记忆 → 守门触发一次重试，最终采用英文结果。"""
    import json

    import httpx

    from sgme import config as sgme_config
    from sgme.engine import l1 as l1_mod

    cfg = sgme_config.load_config()
    dim_id = cfg["dimensions"][0]["id"]

    def body(text: str) -> str:
        return json.dumps([{"content": text, "dimensions": [dim_id],
                            "memory_type": "episodic", "priority": 70,
                            "time_velocity": "static"}], ensure_ascii=False)

    bodies = [body("用户已经收集复古相机三个月了。"),
              body("User has been collecting vintage cameras for three months.")]
    calls: list[dict] = []

    def handler(req):
        calls.append(json.loads(req.content.decode("utf-8")))
        idx = min(len(calls) - 1, 1)
        return httpx.Response(200, json={"choices": [{"message": {"content": bodies[idx]}}]})

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    memories, _provider, _meta = l1_mod.extract_l1(EN_CONV, cfg["dimensions"], cfg["llm"], client=cli)

    assert len(calls) == 2, f"应重试一次，实际调用 {len(calls)} 次"
    assert "vintage cameras" in memories[0]["content"]  # 采用重试后的英文结果
    assert "与会话原文相同的语言" in calls[1]["messages"][0]["content"]  # 重试带语言提示

