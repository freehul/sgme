"""tests/test_conversation_state.py：角色冷场会话状态判断与修补闸（D-72）测试。

覆盖：三条冷场判据各一组正例、正常对话不判冷场、修补闸 already_repaired、
repair_hint 确定性、空历史不崩、signals 结构与数值。
"""
from __future__ import annotations

import pytest

from sgme.engine.conversation_state import assess, repair_hint, should_repair


def _u(text: str) -> dict:
    """构造 user 消息。"""
    return {"role": "user", "content": text}


def _a(text: str) -> dict:
    """构造 assistant 消息。"""
    return {"role": "assistant", "content": text}


# 助手超长回复（>=100 字，判据 2 用）
_LONG_A = "好" * 120
_LONG_A2 = "好" * 110
# 助手中等回复（<100 字，判据 2 负例用）
_MID_A = "好" * 90

# 判据 2 正例历史：最近 4 条 = 用户(2字)/助手(120)/用户(9字)/助手(110)
_MONOLOGUE_HISTORY = [
    _u("今天我们聊点什么呢"),
    _a("都行呀，你想聊什么都可以。"),
    _u("嗯哪"),
    _a(_LONG_A),
    _u("那你继续说吧没关系"),
    _a(_LONG_A2),
]

# 正常对话：长度适中、有问有答，不应判冷场
_NORMAL_HISTORY = [
    _u("今天我们聊聊你上次说的那个旅行计划吧"),
    _a("好呀！我最近确实在纠结要不要去云南，你看这个季节合适吗？"),
    _u("十一月去大理挺舒服的，人少天气也好，就是早晚温差大"),
    _a("那带件外套就行。对了你觉得住古城里还是洱海边比较好？"),
    _u("住洱海边吧，早上拉开窗帘就能看到日出，想想就很值"),
]


# ---------- 判据 1：最近 2 条用户消息都短且不含问号 ----------

class TestCriterionShortReplies:
    def test_two_short_user_replies_trigger_cold(self):
        """最近两条用户消息「嗯」「哦」→ 冷场，reason=short_replies。"""
        history = [
            _u("今天聊聊你最近在忙什么"),
            _a("我最近在整理旧照片，翻到好多高中时候的合影，挺感慨的。"),
            _u("嗯"),
            _a("是吗，那几张照片里你还留着当年的校服呢。"),
            _u("哦"),
        ]
        state = assess(history)
        assert state["is_cold"] is True
        assert state["reason"] == "short_replies"

    def test_question_mark_blocks_criterion(self):
        """最近用户消息带问号 → 判据 1 不触发（其余判据也不命中）。"""
        history = [
            _u("我们出去走走怎么样"),
            _a("好呀，你想去哪？"),
            _u("咋办"),
            _a("你说去哪就去哪，我都可以。"),
            _u("然后呢?"),
        ]
        state = assess(history)
        assert state["is_cold"] is False

    def test_second_to_last_user_too_long_not_cold(self):
        """倒数第二条用户消息 7 字 > 6 → 判据 1 不触发（「嗯嗯」也不在寒暄词表）。"""
        history = [
            _u("周末有什么安排"),
            _a("还没想好，可能在家待着。"),
            _u("等我想想再说吧"),
            _a("行，不急。"),
            _u("嗯嗯"),
        ]
        state = assess(history)
        assert state["is_cold"] is False


# ---------- 判据 2：用户平均短、助手平均长（答得多问得少） ----------

class TestCriterionAssistantMonologue:
    def test_user_avg_le8_and_assistant_avg_ge100_triggers(self):
        """最近 4 条：用户均长 5.5<=8 且助手均长 115>=100 → 冷场。"""
        state = assess(_MONOLOGUE_HISTORY)
        assert state["is_cold"] is True
        assert state["reason"] == "assistant_monologue"

    def test_assistant_avg_below_threshold_not_cold(self):
        """助手均长 90 < 100 → 判据 2 不触发。"""
        history = [_u("嗯哪"), _a(_MID_A), _u("那你继续说吧没关系"), _a(_MID_A)]
        state = assess(history)
        assert state["is_cold"] is False

    def test_user_avg_above_threshold_not_cold(self):
        """用户均长 9 > 8 → 判据 2 不触发（判据 1 也因 9>6 不触发）。"""
        history = [
            _u("等下先听你讲完这段"),
            _a(_LONG_A),
            _u("你继续说吧我听着呢"),
            _a(_LONG_A2),
        ]
        state = assess(history)
        assert state["is_cold"] is False


# ---------- 判据 3：最近用户消息命中寒暄词表 ----------

class TestCriterionGreetingFiller:
    def test_exact_greeting_word_triggers(self):
        """最近用户消息「在吗」完全匹配词表 → 冷场。"""
        history = [
            _u("这周末有空吗我想问你个事"),
            _a("有呀，怎么了？"),
            _u("在吗"),
        ]
        state = assess(history)
        assert state["is_cold"] is True
        assert state["reason"] == "greeting_filler"

    def test_greeting_with_trailing_punctuation_matches(self):
        """「好的，」只差标点 → 仍命中词表。"""
        history = [
            _u("问你个事想听听你意见"),
            _a("你说。"),
            _u("好的，"),
        ]
        state = assess(history)
        assert state["is_cold"] is True
        assert state["reason"] == "greeting_filler"

    def test_non_listed_short_reply_not_greeting(self):
        """「洗洗睡了啊」不在词表 → 判据 3 不触发（判据 1 因前一条长也不触发）。"""
        history = [
            _u("上次你说的那家店叫什么来着"),
            _a("就我家附近那家川菜馆，怎么你想起来了？"),
            _u("洗洗睡了啊"),
        ]
        state = assess(history)
        assert state["is_cold"] is False


# ---------- 正常对话 / 边界历史 ----------

class TestNotColdAndEdgeCases:
    def test_normal_conversation_not_cold(self):
        """有问有答的正常对话 → 不判冷场，reason 为空串。"""
        state = assess(_NORMAL_HISTORY)
        assert state["is_cold"] is False
        assert state["reason"] == ""

    def test_empty_history_not_crash(self):
        """空历史不崩：不冷场，signals 全零。"""
        state = assess([])
        assert state["is_cold"] is False
        assert state["reason"] == ""
        assert state["signals"] == {
            "user_avg_len": 0.0,
            "assistant_avg_len": 0.0,
            "last_user_len": 0,
        }

    def test_single_user_message_needs_two_for_criterion1(self):
        """只有 1 条用户消息 → 判据 1 不触发（需最近 2 条）。"""
        state = assess([_u("洗洗睡")])
        assert state["is_cold"] is False

    def test_assistant_only_history_not_cold(self):
        """只有 assistant 消息（无用户发言）→ 不判冷场。"""
        state = assess([_a("好" * 150)])
        assert state["is_cold"] is False


# ---------- signals 结构 ----------

class TestSignals:
    def test_signals_values_match_recent_window(self):
        """signals 按最近 4 条窗口计算，数值与判据 2 口径一致。"""
        state = assess(_MONOLOGUE_HISTORY)
        signals = state["signals"]
        assert set(signals) == {"user_avg_len", "assistant_avg_len", "last_user_len"}
        assert signals["user_avg_len"] == pytest.approx((2 + 9) / 2)
        assert signals["assistant_avg_len"] == pytest.approx((120 + 110) / 2)
        assert signals["last_user_len"] == len("那你继续说吧没关系")

    def test_signals_on_normal_history(self):
        """正常对话的 last_user_len 取最近一条用户消息长度。"""
        state = assess(_NORMAL_HISTORY)
        assert state["signals"]["last_user_len"] == len(
            "住洱海边吧，早上拉开窗帘就能看到日出，想想就很值"
        )


# ---------- 修补闸 should_repair ----------

class TestShouldRepair:
    def _cold_state(self) -> dict:
        return assess([
            _u("我们聊聊最近的安排吧"),
            _a("好呀，你想聊什么？"),
            _u("嗯"),
            _a("嗯？怎么了，今天感觉怎么样？"),
            _u("哦"),
        ])

    def test_cold_and_not_repaired(self):
        assert should_repair(self._cold_state(), already_repaired=False) is True

    def test_cold_but_already_repaired(self):
        """同一信号最多修补 1 次：已修补过 → 不再修补。"""
        assert should_repair(self._cold_state(), already_repaired=True) is False

    def test_not_cold_never_repairs(self):
        assert should_repair(assess(_NORMAL_HISTORY), already_repaired=False) is False


# ---------- repair_hint 确定性 ----------

class TestRepairHint:
    def _reason_states(self) -> dict:
        return {
            "short_replies": assess([
                _u("我们聊聊最近的安排吧"),
                _a("好呀，你想聊什么？"),
                _u("嗯"),
                _a("嗯？怎么了，今天感觉怎么样？"),
                _u("哦"),
            ]),
            "assistant_monologue": assess(_MONOLOGUE_HISTORY),
            "greeting_filler": assess([
                _u("这周末有空吗我想问你个事"),
                _a("有呀，怎么了？"),
                _u("在吗"),
            ]),
        }

    def test_hint_nonempty_and_distinct_per_reason(self):
        """三种 reason 都有非空提示且两两不同。"""
        hints = {reason: repair_hint(state) for reason, state in self._reason_states().items()}
        for reason, hint in hints.items():
            assert hint, f"{reason} 的提示不应为空"
        values = list(hints.values())
        assert len(set(values)) == 3

    def test_hint_deterministic(self):
        """同一 state（及同一历史重复 assess）→ 同一字符串。"""
        state = self._reason_states()["short_replies"]
        assert repair_hint(state) == repair_hint(state)
        assert repair_hint(state) == repair_hint(assess(state and [
            _u("我们聊聊最近的安排吧"),
            _a("好呀，你想聊什么？"),
            _u("嗯"),
            _a("嗯？怎么了，今天感觉怎么样？"),
            _u("哦"),
        ]))

    def test_hint_empty_when_not_cold(self):
        assert repair_hint(assess(_NORMAL_HISTORY)) == ""
        assert repair_hint({"is_cold": False, "reason": ""}) == ""
