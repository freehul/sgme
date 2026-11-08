"""v023 提示词契约：按钮样本 causes 的「助手动作句 / 截短前缀」禁令（W09d，2026-11-09）。

背景
----
跨模型复测（W09c）发现 zhipu 按钮样本 causes 端点 exact 仅 75%，两类失败：
  ① to 写成助手动作/结果句（「已把提交按钮改成白字，底色用实色蓝」）——不在 main/facts；
  ② from 被截短成主干前缀（「演示页提交按钮黑字看不清」vs 主干含「，已改为白字配实色蓝」）。
v023 在 v022 基础上明确禁止这两类，并补「按钮+助手动作」完整正反例与输出前五问。

本测试锁：
1. manifest active 指向 v023（最新登记版本）、sha256 与文件一致、工作副本逐字一致；
2. v023 含助手动作句禁令、截短/前缀禁令、五问自检、按钮完整正反例；
3. v023 保留 v022 causes 逐字硬约束、v021 UI 口径、v020 结构硬约束；
4. v023 不新增占位符（提示词改动不引入新渲染变量/新 LLM 调用）；
5. v024 及以后默认继承 v023 的两条禁令（防再次分叉丢失）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "sgme" / "resources" / "prompts"
MANIFEST = PROMPTS_ROOT / "manifest.yaml"
VERSIONS_DIR = PROMPTS_ROOT / "versions" / "l1_extraction"

# v023 新增禁令锚点（缺任一条视为口径丢失）
STRICT_ANCHORS = (
    "禁止助手动作/结果句",
    "禁止截短/前缀/概括/省略后半句",
    "主干不得混入请求/动作语",
    "输出前逐条过五问",
    "❌ 助手动作句（对话里出现过但不是 main/facts）",
    "❌ 截短前缀",
    "✅ 按钮正确写法（助手动作不写进 main/facts，也不写进 causes）",
    "按钮反馈（对话里有助手动作「已把提交按钮改成白字，底色用实色蓝」",
    "不得截短/只取前缀",
)


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _versions_at_least(minimum: int) -> list[dict]:
    out = []
    for item in _manifest()["versions"]["l1_extraction"]:
        tag = item["version"]
        assert tag.startswith("v") and tag[1:].isdigit(), f"非法版本号 {tag}"
        if int(tag[1:]) >= minimum:
            out.append(item)
    return out


def test_manifest_active_is_v023():
    """manifest active 必须钉在 v023（本次交付的显式发布决策）。"""
    active = _manifest()["stages"]["l1_extraction"]["active"]
    assert active == "v023", f"active={active}，期望 v023"


def test_v023_registered_and_sha_matches():
    """v023 已登记 manifest，且 sha256 与文件内容一致（防手改后忘记重登记）。"""
    entries = {v["version"]: v for v in _versions_at_least(23)}
    assert "v023" in entries, "manifest 未登记 v023"
    path = PROMPTS_ROOT / entries["v023"]["file"]
    assert path.exists(), f"v023 文件不存在: {path}"
    digest = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert digest == entries["v023"]["sha256"], (
        f"v023 sha256 不一致：manifest={entries['v023']['sha256'][:12]} 实际={digest[:12]}"
    )


def test_working_copy_equals_v023():
    """工作副本必须与 v023 逐字一致（否则 @working 会回退成旧口径）。"""
    v023 = (VERSIONS_DIR / "v023.txt").read_text(encoding="utf-8")
    working = (PROMPTS_ROOT / "l1_extraction.txt").read_text(encoding="utf-8")
    assert working == v023, "工作副本与 v023 不一致"


@pytest.mark.parametrize("anchor", STRICT_ANCHORS)
def test_v023_contains_strict_causes_rules(anchor):
    """v023 必须含两条禁令 + 按钮正反例 + 五问自检（逐条断言）。"""
    text = (VERSIONS_DIR / "v023.txt").read_text(encoding="utf-8")
    assert anchor in text, f"v023 缺少严格 causes 锚点：{anchor}"


def test_v023_keeps_v022_causes_contract():
    """v023 不得丢失 v022 的逐字复制硬约束与映射最稳写法。"""
    text = (VERSIONS_DIR / "v023.txt").read_text(encoding="utf-8")
    for anchor in (
        "causes 端点必须逐字引用原文",
        "不改写、不摘要、不拼接、不补标点",
        "不输出这条 cause",
        "推荐写法（映射最稳）",
        "10. causes 的 from/to 必须逐字复制 main_event.content 或某条 facts[].content",
    ):
        assert anchor in text, f"v023 丢失 v022 causes 锚点：{anchor}"


def test_v023_keeps_v021_ui_and_v020_structure():
    """v023 不得丢失 v021 UI 口径与 v020 结构硬约束。"""
    text = (VERSIONS_DIR / "v023.txt").read_text(encoding="utf-8")
    for anchor in (
        "一次性 UI/操作反馈",
        "❌ 裸事实：「用户反馈提交按钮黑字看不清，要求改成白色。」",
        "✅ 规则：「深色底黑字对比度不足 → 界面按钮统一白字配实色底，禁用低对比方案。」",
        "结构是硬性的，不是建议",
        "禁止**输出平铺数组",
        "禁止平铺数组——结构错了，2.0 字段全部作废",
    ):
        assert anchor in text, f"v023 丢失旧约束：{anchor}"


def test_v023_no_new_placeholders():
    """v023 不得新增占位符：占位符集合须与 v022 完全相同（防新渲染变量/新 LLM 调用）。"""

    def placeholders(text: str) -> set[str]:
        return set(re.findall(r"\{\{[a-z_]+\}\}", text))

    v022 = (VERSIONS_DIR / "v022.txt").read_text(encoding="utf-8")
    v023 = (VERSIONS_DIR / "v023.txt").read_text(encoding="utf-8")
    assert placeholders(v023) == placeholders(v022), (
        f"v023 占位符集合变化：{sorted(placeholders(v023) ^ placeholders(v022))}"
    )


def test_later_versions_inherit_strict_causes_rules():
    """v023 及以后的每个版本都必须保留两条禁令（防再次分叉丢失）。"""
    versions = _versions_at_least(23)
    assert versions, "未找到 v023 及以后的 l1_extraction 版本"
    for item in versions:
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        for anchor in STRICT_ANCHORS:
            assert anchor in text, f"{item['version']} 丢失严格 causes 锚点：{anchor}"
