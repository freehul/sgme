"""ST-58/T-300（B1'）：候选元数据行裁剪——只压缩**呈现体积**，不动候选集与正文。

与 B2（候选数收紧 30/30）的区别是根本性的，也是本文件要锁死的安全不变量：
- B2 让相关候选**掉出输入** → 已实测导致漏判（B255，门禁不通过）；
- B1' 只让输入**变短** → 候选 id 与正文全在，模型仍看得到每一条。

故核心断言是 **`[候选#…]` 条数在 full / minimal 下完全一致**（没有候选被丢掉）。
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.engine import l15


# ---------- resolve_candidate_fields ----------


def test_default_is_full_preset():
    """缺配置 → full（现状，零回归）。"""
    f = l15.resolve_candidate_fields(None)
    assert f == {"show_dimensions": True, "show_memory_type": True,
                 "show_priority": True, "updated_at_format": "iso"}
    assert l15.resolve_candidate_fields({}) == f
    assert l15.resolve_candidate_fields({"candidate_fields": "garbage"}) == f


def test_minimal_preset_flags():
    f = l15.resolve_candidate_fields({"candidate_fields": {"preset": "minimal"}})
    assert f["show_dimensions"] is False
    assert f["show_memory_type"] is False
    assert f["show_priority"] is False
    assert f["updated_at_format"] == "date"


def test_explicit_switch_overrides_preset():
    """单字段开关覆盖 preset —— 支持三因子单变量对照（如只砍 dimensions）。"""
    f = l15.resolve_candidate_fields({
        "candidate_fields": {"preset": "minimal", "show_priority": True},
    })
    assert f["show_dimensions"] is False      # 跟随 preset
    assert f["show_priority"] is True         # 显式覆盖


def test_illegal_values_fall_back():
    """非法值一律退回默认，不静默放行错配置。"""
    f = l15.resolve_candidate_fields({"candidate_fields": {
        "preset": "nope", "show_dimensions": "yes", "updated_at_format": "epoch",
    }})
    assert f["show_dimensions"] is True          # 非 bool → 不生效
    assert f["updated_at_format"] == "iso"       # 非法格式 → 不生效
    # preset 非法 → 整体退回 full
    assert f["show_priority"] is True


def test_config_merge_carries_candidate_fields():
    """配置层合并（sgme.yaml → cfg['l15']）也要带上并做同样校验。"""
    base = config._merge_l15_config(None)
    assert base["candidate_fields"]["preset"] == "full"
    merged = config._merge_l15_config({"candidate_fields": {"preset": "minimal"}})
    assert merged["candidate_fields"]["preset"] == "minimal"
    bad = config._merge_l15_config({"candidate_fields": {"preset": "nope"}})
    assert bad["candidate_fields"]["preset"] == "full"


# ---------- _format_candidates：体积与不变量 ----------


def _cands(n: int = 3) -> list[dict]:
    return [{
        "memory_id": f"m{i}",
        "content": f"第 {i} 条候选的正文内容，含一些中文描述用于估算体积",
        "tags": ["tech_stack", "habits"],
        "memory_type": "fact",
        "priority": 50 + i,
        "updated_at": "2026-05-0%dT00:00:00Z" % (i + 1),
    } for i in range(n)]


def test_full_output_contains_all_metadata():
    out = l15._format_candidates(_cands(1))
    assert "dimensions: ['tech_stack', 'habits']" in out
    assert "memory_type: fact" in out
    assert "priority: 50" in out
    assert "2026-05-01T00:00:00Z" in out


def test_minimal_output_keeps_content_and_drops_metadata():
    out = l15._format_candidates(_cands(1), l15.resolve_candidate_fields(
        {"candidate_fields": {"preset": "minimal"}}))
    assert "content: 第 0 条候选的正文内容" in out     # 正文必须在
    assert "dimensions:" not in out
    assert "memory_type:" not in out
    assert "priority:" not in out
    assert "updated_at: 2026-05-01" in out            # 短日期保留（模板第 6 条平局线索）
    assert "T00:00:00Z" not in out


def test_candidate_count_unchanged_between_presets():
    """**核心安全不变量**：B1' 不得丢候选（这正是它与 B2 的分界）。"""
    cands = _cands(7)
    full = l15._format_candidates(cands)
    minimal = l15._format_candidates(
        cands, l15.resolve_candidate_fields({"candidate_fields": {"preset": "minimal"}}))
    assert full.count("[候选#") == minimal.count("[候选#") == 7
    for i in range(7):
        assert f"[候选#m{i}]" in full and f"[候选#m{i}]" in minimal


def test_minimal_is_shorter_than_full():
    """确实省体积（否则方案收益不成立）。"""
    cands = _cands(20)
    full = l15._format_candidates(cands)
    minimal = l15._format_candidates(
        cands, l15.resolve_candidate_fields({"candidate_fields": {"preset": "minimal"}}))
    assert len(minimal) < len(full)
    savings = 1 - len(minimal) / len(full)
    assert savings > 0.20, f"降幅仅 {savings:.1%}，与方案实测（约 31%）不符量级"


def test_date_format_handles_empty_or_short_timestamp():
    """updated_at 缺失/异常短 → 不得抛异常（候选可能来自老库）。"""
    cands = [{"memory_id": "m0", "content": "x", "updated_at": ""}]
    out = l15._format_candidates(cands, l15.resolve_candidate_fields(
        {"candidate_fields": {"preset": "minimal"}}))
    assert "updated_at: " in out


# ---------- render_l15 向后兼容 ----------


def test_render_l15_backward_compatible_default():
    """不传 candidate_fields 时输出与 full 一致（既有调用方零回归）。"""
    import io
    from pathlib import Path

    tmpl = Path("sgme/resources/prompts/l1_conflict.txt")
    if not tmpl.exists():
        pytest.skip("提示词工作副本缺失")
    text = io.open(tmpl, encoding="utf-8").read()
    nm = [{"content": "新记忆", "dimension_ids": ["habits"], "memory_type": "fact"}]
    a = l15._render_l15_text(text, nm, _cands(2))
    b = l15._render_l15_text(text, nm, _cands(2), None)
    assert a == b
    assert "dimensions:" in a
