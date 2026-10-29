"""L1 解析层护栏测试（2026-10-29 四轮/五轮实测驱动，D-66/67/68 落地前置）。

背景（都有实测证据）：
1. **P0 阻断**：v009/v010 提示词要 `dimensions: {primary, secondary}`（2.0 schema），
   而 1.x `_validate_item` 只认 list → 拿 v009 真实产物实测 **0/20 通过**，
   直接激活会让提炼静默归零。
2. 软遗忘：A/B 实测模型把被忘内容原样写进正文 **22/22 违反** → 必须代码层强制重写。
3. 语言：块级守门会放过混合块里的个别中文条目 → 需逐条判。
4. type：实测出现非法值「计划」。
5. 敏感：v009 产物里存了**原始邮箱地址**（`Email address is ame...@...`）→ 必须脱敏。
6. subject：D-66 要求必填，缺失时归 `unknown` 并计数，不得整条丢弃。
"""

from __future__ import annotations

import json

from sgme.engine.l1 import (
    DOMAIN_TYPES,
    _normalize_dimensions,
    _parse_item,
    _redact_sensitive,
    _validate_item,
    parse_l1_output,
    reset_violation_counts,
    violation_counts,
)

DIMS = [
    {"id": "identity", "display_name": "身份", "active": 1, "time_velocity": "static"},
    {"id": "health", "display_name": "健康", "active": 1, "time_velocity": "dynamic"},
    {"id": "habits", "display_name": "习惯", "active": 1, "time_velocity": "dynamic"},
    {"id": "preferences", "display_name": "偏好", "active": 1, "time_velocity": "static"},
]


def _item(**kw):
    base = {
        "content": "User has been collecting vintage cameras for three months.",
        "dimensions": ["identity"],
        "memory_type": "persona",
        "priority": 70,
    }
    base.update(kw)
    return base


# ---------- 1. 维度双形态（P0）----------

def test_accepts_2_0_dict_dimensions():
    """v009/v010 的 dimensions 是 {primary, secondary} → 必须接受并合并成 list。"""
    it = _item(dimensions={"primary": "identity", "secondary": ["health", "habits"]})
    out = _validate_item(it, DIMS)
    assert out is not None
    assert out["dimensions"] == ["identity", "health", "habits"]


def test_accepts_legacy_list_dimensions():
    out = _validate_item(_item(dimensions=["identity", "health"]), DIMS)
    assert out is not None
    assert out["dimensions"] == ["identity", "health"]


def test_dict_dimensions_without_secondary():
    out = _validate_item(_item(dimensions={"primary": "identity"}), DIMS)
    assert out is not None
    assert out["dimensions"] == ["identity"]


def test_unknown_dimension_ids_are_counted_not_dropped():
    """未知维度只计数，不连坐整条记忆（丢弃由 refine 侧别名表负责）。"""
    out = _validate_item(_item(dimensions={"primary": "identity", "secondary": ["nope"]}), DIMS)
    assert out is not None
    assert out["dimensions"] == ["identity", "nope"]


def test_item_with_no_valid_dimension_is_rejected():
    assert _validate_item(_item(dimensions={"primary": "nope"}), DIMS) is not None
    assert _validate_item(_item(dimensions=[]), DIMS) is None
    assert _validate_item(_item(dimensions={"secondary": []}), DIMS) is None


def test_normalize_dimensions_dedupes_and_reports_unknown():
    dims, dropped = _normalize_dimensions(
        {"primary": "identity", "secondary": ["identity", "health", "nope"]}, DIMS
    )
    assert dims == ["identity", "health", "nope"]
    assert dropped == 1


# ---------- 2. 软遗忘正文强制重写 ----------

def test_forget_content_is_rewritten_and_original_not_kept():
    """模型 22/22 把被忘内容写进正文 → 解析层必须重写，且不保留原文。"""
    it = _parse_item(
        _item(
            content="User had a past ankle ligament sprain from five-a-side football.",
            memory_type="instruction",
            forget={"topic": "past ankle ligament sprain", "kind": "event"},
        ),
        DIMS,
    )
    assert it is not None
    low = it["content"].lower()
    assert "ankle" not in low and "football" not in low
    assert "no longer be actively mentioned" in low
    assert it["forget"]["topic"] == "past ankle ligament sprain"
    # 原文只留指纹，不留正文
    assert it["forget"].get("redacted_sha256")
    assert "ankle" not in str(it["forget"].get("redacted_sha256", "")).lower()


def test_forget_without_topic_falls_back_to_generic_phrase():
    it = _parse_item(
        _item(content="用户不想再提脚踝受伤的事", memory_type="instruction"),
        DIMS,
    )
    assert it is not None
    assert "脚踝" not in it["content"]
    assert it["memory_type"] == "instruction"


def test_normal_item_content_untouched():
    it = _parse_item(_item(content="User owns 17 vintage cameras."), DIMS)
    assert it is not None
    assert it["content"] == "User owns 17 vintage cameras."


# ---------- 3. type 七值白名单 ----------

def test_valid_types_pass_through():
    for t in DOMAIN_TYPES:
        out = _validate_item(_item(type=t), DIMS)
        assert out is not None
        assert out["type"] == t


def test_invalid_type_falls_back_not_silently_kept():
    """实测出现非法值「计划」→ 不得原样留存。"""
    out = _validate_item(_item(type="计划", modality="planned"), DIMS)
    assert out is not None
    assert "计划" not in DOMAIN_TYPES
    assert out["type"] in DOMAIN_TYPES


def test_missing_type_defaults_to_fact():
    out = _validate_item(_item(), DIMS)
    assert out is not None
    assert out["type"] in DOMAIN_TYPES


# ---------- 4. 敏感标识符脱敏 ----------

def test_email_is_redacted():
    text, hit = _redact_sensitive("Email address is amahle.ncube@yorkshiresportmedia.co.uk.")
    assert hit is True
    assert "amahle.ncube" not in text
    assert "<REDACTED" in text


def test_phone_ssn_and_document_numbers_are_redacted():
    for raw in (
        "my number is +1 415 555 0132",
        "ssn: 382-67-4915",
        "drivers_license_number: DL-KEN-4928371",
    ):
        text, hit = _redact_sensitive(raw)
        assert hit is True, raw
        assert "<REDACTED" in text


def test_plain_text_is_not_flagged():
    text, hit = _redact_sensitive("User takes phone snapshots at markets.")
    assert hit is False
    assert text == "User takes phone snapshots at markets."


def test_item_body_is_redacted_via_validate():
    out = _validate_item(
        _item(content="User's email is leo.example@example.com for the newsletter."), DIMS
    )
    assert out is not None
    assert "leo.example@example.com" not in out["content"]


def test_item_that_is_only_sensitive_is_dropped():
    assert _validate_item(_item(content="ssn: 382-67-4915"), DIMS) is None


# ---------- 5. subject / certainty 必填 ----------

def test_subject_missing_becomes_unknown():
    out = _validate_item(_item(), DIMS)
    assert out is not None
    assert out["subject"] == "unknown"


def test_subject_others_kept():
    out = _validate_item(_item(subject="others:James"), DIMS)
    assert out is not None
    assert out["subject"] == "others:James"


def test_certainty_missing_defaults_confirmed_for_fact_derivation():
    out = _validate_item(_item(derivation="fact"), DIMS)
    assert out is not None
    assert out["certainty"] in {"confirmed", "tentative", "discussing"}


# ---------- 6. 逐条语言判定 ----------

def test_item_lang_drift_only_drops_the_bad_item():
    from sgme.engine.l1 import _item_lang_drift

    assert _item_lang_drift({"content": "用户已经收集复古相机三个月了。"}, english_session=True) is True
    assert _item_lang_drift({"content": "User has collected vintage cameras."}, english_session=True) is False
    # 中文会话不判
    assert _item_lang_drift({"content": "User has collected vintage cameras."}, english_session=False) is False


def test_parse_output_drops_only_the_chinese_item_in_english_session():
    """混合块：英文会话里夹一条中文记忆 → 只丢那一条，其余照留（并计数）。"""
    from sgme.engine.l1 import _drop_lang_drift_items

    en_conv = ("user: I have been collecting vintage cameras for months now.\n"
               "assistant: Lovely hobby, which models do you own?\n") * 8
    payload = json.dumps([
        {"content": "User has collected vintage cameras for months.",
         "dimensions": ["preferences"], "memory_type": "persona", "priority": 70},
        {"content": "用户已经收集复古相机三个月了。",
         "dimensions": ["preferences"], "memory_type": "persona", "priority": 70},
    ])
    reset_violation_counts()
    memories = parse_l1_output(payload, DIMS)
    kept = _drop_lang_drift_items(memories, en_conv)
    assert len(kept) == 1
    assert "vintage cameras" in kept[0]["content"]
    assert violation_counts()["lang_drift_dropped"] == 1
