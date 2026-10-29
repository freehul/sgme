"""内置角色卡的发布门禁（回归护栏）。

背景（2026-10-29 线上实测）：
- 问「你是谁」时四张卡全答「我是 Agnes，由 Sapiens AI 开发」——卡里没有「身份应答」，
  模型自我认同压过角色；
- 第一版补丁仍自称「我是你的专属管家 Agnes」，并编造「下午三点的数据复盘」。

本文件把这两条钉成机器门禁：内置卡必须①合法 ②带身份应答 ③不出现模型/厂商名。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sgme.care.roles import validate_role_card

ROLES_DIR = Path(__file__).resolve().parent.parent / "roles"
BUILTIN = ["butler", "companion", "friend", "mentor"]

# 一旦出现在角色卡里，模型就会拿它当自己的名字或自称厂商
FORBIDDEN_NAMES = [
    "Agnes",
    "Sapiens",
    "OpenAI",
    "ChatGPT",
    "GPT-4",
    "GPT-5",
    "Anthropic",
    "Claude",
    "DeepSeek",
    "通义",
    "千问",
    "文心",
    "豆包",
    "Kimi",
    "Gemini",
    "Llama",
]


def _card(role_id: str) -> dict:
    return json.loads((ROLES_DIR / f"{role_id}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("role_id", BUILTIN)
def test_builtin_card_is_valid(role_id: str) -> None:
    """四张内置卡必须过 CC V2 子集校验。"""
    assert validate_role_card(_card(role_id)) == []


@pytest.mark.parametrize("role_id", BUILTIN)
def test_builtin_card_has_identity_slot(role_id: str) -> None:
    """身份应答槽必填：没有它，模型被问「你是谁」就自报模型名。"""
    sp = _card(role_id)["data"].get("system_prompt") or ""
    assert "【身份应答】" in sp, f"{role_id} 缺身份应答槽"
    assert "不报模型名" in sp, f"{role_id} 身份应答未禁止自报模型名"


@pytest.mark.parametrize("role_id", BUILTIN)
def test_builtin_card_has_no_model_or_vendor_name(role_id: str) -> None:
    """卡内出现模型名/厂商名 → 模型会把它当成自己的名字（实测踩过）。"""
    data = _card(role_id)["data"]
    blob = json.dumps(data, ensure_ascii=False)
    hits = [n for n in FORBIDDEN_NAMES if n.lower() in blob.lower()]
    assert hits == [], f"{role_id} 出现模型/厂商名：{hits}"
