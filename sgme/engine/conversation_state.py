"""engine/conversation_state.py：角色冷场会话状态判断与修补闸（D-72）。

纯函数模块（无 IO / 无 LLM），供注入层在角色连续收到短回复时决定是否
触发一次「修补」（换话题 / 主动抛料）。

冷场判据（满足任一即 is_cold=True，按优先级给出唯一 reason）：
1. short_replies：最近 2 条用户消息都 <=6 字符且不含问号
2. assistant_monologue：最近 4 条里用户消息平均长度 <=8 且 assistant
   消息平均长度 >=100（答得多问得少）
3. greeting_filler：最近用户消息命中寒暄词表（完全匹配或只差标点）

修补闸：同一冷场信号最多修补 1 次——should_repair(state, already_repaired)
仅当 is_cold 且未修补过才放行。repair_hint 按 reason 给确定性中文提示
（同一 reason 恒返回同一字符串，便于测试与上层缓存）。
"""
from __future__ import annotations

import string

# 判据 1：最近 N 条用户消息长度阈值（字符数，<=）
SHORT_USER_LEN = 6
# 判据 2：窗口大小（最近 4 条消息）
RECENT_WINDOW = 4
# 判据 2：窗口内用户消息平均长度上限（<=）
USER_AVG_LEN_MAX = 8
# 判据 2：窗口内 assistant 消息平均长度下限（>=）
ASSISTANT_AVG_LEN_MIN = 100

# 寒暄词表（完全匹配或只差标点命中）
GREETING_FILLERS = frozenset({
    "嗯", "哦", "好的", "好", "随便", "都行", "是吗", "哈哈", "在吗",
})

# 去除后参与词表比对的标点（中英文常用标点 + 空白）
_PUNCT = set(string.punctuation) | set("，。！？；：、""''（）《》…—·～ \t\r\n")


def _strip_punct(text: str) -> str:
    """去掉首尾标点与空白（只差标点即视为同一词）。"""
    return text.strip("".join(_PUNCT))


def _msg_len(content: str) -> int:
    """消息长度（按字符数，不做去空格等归一）。"""
    return len(content)


def _avg_len(messages: list[dict]) -> float:
    """消息列表的平均 content 长度；空列表返回 0.0。"""
    if not messages:
        return 0.0
    return sum(_msg_len(m.get("content") or "") for m in messages) / len(messages)


def assess(history: list[dict]) -> dict:
    """判断会话是否冷场。

    - history：会话消息列表，每条 {"role": "user"|"assistant", "content": str}，
      按时间正序
    - 返回 {"is_cold": bool, "reason": str, "signals": dict}
      reason ∈ {""|short_replies|assistant_monologue|greeting_filler}
      signals = {"user_avg_len": float, "assistant_avg_len": float,
                 "last_user_len": int}（按最近 RECENT_WINDOW 条窗口计算；
      无用户消息时 user_avg_len 为 0.0，last_user_len 为 0）
    """
    user_msgs = [m for m in history if m.get("role") == "user"]
    assistant_msgs = [m for m in history if m.get("role") == "assistant"]
    recent = history[-RECENT_WINDOW:]
    recent_users = [m for m in recent if m.get("role") == "user"]
    recent_assistants = [m for m in recent if m.get("role") == "assistant"]

    signals = {
        "user_avg_len": _avg_len(recent_users),
        "assistant_avg_len": _avg_len(recent_assistants),
        "last_user_len": _msg_len(user_msgs[-1]["content"]) if user_msgs else 0,
    }

    # 判据 1：最近 2 条用户消息都短且不含问号
    last_two_users = [m.get("content") or "" for m in user_msgs[-2:]]
    if (len(last_two_users) == 2
            and all(_msg_len(c) <= SHORT_USER_LEN and "?" not in c and "？" not in c
                    for c in last_two_users)):
        return {"is_cold": True, "reason": "short_replies", "signals": signals}

    # 判据 2：答得多问得少（窗口内用户均短、assistant 均长）
    if (recent_users and recent_assistants
            and signals["user_avg_len"] <= USER_AVG_LEN_MAX
            and signals["assistant_avg_len"] >= ASSISTANT_AVG_LEN_MIN):
        return {"is_cold": True, "reason": "assistant_monologue", "signals": signals}

    # 判据 3：最近用户消息命中寒暄词表（完全匹配或只差标点）
    if user_msgs:
        last_user = user_msgs[-1]["content"] or ""
        stripped = _strip_punct(last_user)
        if last_user in GREETING_FILLERS or stripped in GREETING_FILLERS:
            return {"is_cold": True, "reason": "greeting_filler", "signals": signals}

    return {"is_cold": False, "reason": "", "signals": signals}


def should_repair(state: dict, already_repaired: bool) -> bool:
    """修补闸：is_cold 且未修补过才放行（同一信号最多修补 1 次）。"""
    return bool(state.get("is_cold")) and not already_repaired


# 各 reason 对应的修补提示（确定性：同一 reason 恒返回同一字符串）
_REPAIR_HINTS = {
    "short_replies": "对方回得很短，换个具体话题，别追问",
    "assistant_monologue": "别一个人长篇输出，抛一个具体的小问题把对方拉回来",
    "greeting_filler": "对方只回了寒暄，主动抛出新话题或新消息，别停在原地",
}


def repair_hint(state: dict) -> str:
    """按 reason 返回给注入层的中文修补提示；不冷场返回空串。"""
    if not state.get("is_cold"):
        return ""
    return _REPAIR_HINTS.get(state.get("reason", ""), "")
