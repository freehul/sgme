"""v022 提示词契约：causes 端点必须逐字复制 main/facts 原文（W09b，2026-11-08）。

背景
----
dry-run 实测存量 causes 的 from/to 是摘要句，与任何记忆原文精确匹配 0%；W04 映射器
只做确定性文本归一匹配（不新增 LLM 调用），端点对不上就一条 causes 边也建不出来。
v022 在 v021 基础上加硬约束：causes[].from/to 必须逐字复制同一单元 main_event.content
或某条 facts[].content 的完整原文（不摘要/不改写/不拼接/不补标点）；表达不了就不输出。

本测试锁：
1. manifest active 指向 v022（最新登记版本）、sha256 与文件一致、工作副本逐字一致；
2. v022 含硬约束正文、正反例、逐步自检、映射最稳写法与输出要求第 10 条；
3. v022 保留 v021 的 UI/操作反馈口径与 v020 结构硬约束（不因新增版本丢旧约束）；
4. v022 不新增占位符（提示词改动不引入新渲染变量/新 LLM 调用）；
5. v023 及以后默认继承 causes 硬约束（防再次分叉丢失）。
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

# v022 causes 硬约束锚点（缺任一条视为约束丢失）
CAUSES_ANCHORS = (
    "causes 端点必须逐字引用原文",
    "逐字复制",
    "不改写、不摘要、不拼接、不补标点",
    "不输出这条 cause",
    "自检（逐条 cause）",
    "输出前逐条过三问",
    "端点组合只允许三种",
    "完整示例（照此形状输出",
    "推荐写法（映射最稳）",
    "10. causes 的 from/to 必须逐字复制 main_event.content 或某条 facts[].content",
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


def test_manifest_active_is_v022():
    """manifest active 必须钉在 v022（本次交付的显式发布决策）。"""
    active = _manifest()["stages"]["l1_extraction"]["active"]
    assert active == "v022", f"active={active}，期望 v022"


def test_v022_registered_and_sha_matches():
    """v022 已登记 manifest，且 sha256 与文件内容一致（防手改后忘记重登记）。"""
    entries = {v["version"]: v for v in _versions_at_least(22)}
    assert "v022" in entries, "manifest 未登记 v022"
    path = PROMPTS_ROOT / entries["v022"]["file"]
    assert path.exists(), f"v022 文件不存在: {path}"
    digest = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert digest == entries["v022"]["sha256"], (
        f"v022 sha256 不一致：manifest={entries['v022']['sha256'][:12]} 实际={digest[:12]}"
    )


def test_working_copy_equals_v022():
    """工作副本必须与 v022 逐字一致（否则 @working 会回退成旧口径）。"""
    v022 = (VERSIONS_DIR / "v022.txt").read_text(encoding="utf-8")
    working = (PROMPTS_ROOT / "l1_extraction.txt").read_text(encoding="utf-8")
    assert working == v022, "工作副本与 v022 不一致"


@pytest.mark.parametrize("anchor", CAUSES_ANCHORS)
def test_v022_contains_causes_hard_constraint(anchor):
    """v022 必须含 causes 硬约束与自检锚点（逐条断言，坏在哪条一眼可见）。"""
    text = (VERSIONS_DIR / "v022.txt").read_text(encoding="utf-8")
    assert anchor in text, f"v022 缺少 causes 硬约束锚点：{anchor}"


def test_v022_keeps_v021_ui_feedback_rule():
    """v022 不得丢失 v021 的 UI/操作反馈口径（正反例逐字保留）。"""
    text = (VERSIONS_DIR / "v022.txt").read_text(encoding="utf-8")
    for anchor in (
        "一次性 UI/操作反馈",
        "❌ 裸事实：「用户反馈提交按钮黑字看不清，要求改成白色。」",
        "✅ 规则：「深色底黑字对比度不足 → 界面按钮统一白字配实色底，禁用低对比方案。」",
    ):
        assert anchor in text, f"v022 丢失 v021 UI 反馈锚点：{anchor}"


def test_v022_keeps_v020_structural_contract():
    """v022 不得丢失 v020 的结构硬约束与输出要求第 8 条。"""
    text = (VERSIONS_DIR / "v022.txt").read_text(encoding="utf-8")
    for anchor in (
        "结构是硬性的，不是建议",
        "禁止**输出平铺数组",
        "自检：输出前逐个元素确认有 `main_event` / `dimensions` / `route` 三个键",
        "禁止平铺数组——结构错了，2.0 字段全部作废",
    ):
        assert anchor in text, f"v022 丢失 v020 结构约束：{anchor}"


def test_v022_no_new_placeholders():
    """v022 不得新增占位符：占位符集合须与 v021 完全相同（防新渲染变量/新 LLM 调用）。"""

    def placeholders(text: str) -> set[str]:
        return set(re.findall(r"\{\{[a-z_]+\}\}", text))

    v021 = (VERSIONS_DIR / "v021.txt").read_text(encoding="utf-8")
    v022 = (VERSIONS_DIR / "v022.txt").read_text(encoding="utf-8")
    assert placeholders(v022) == placeholders(v021), (
        f"v022 占位符集合变化：{sorted(placeholders(v022) ^ placeholders(v021))}"
    )


def test_later_versions_inherit_causes_hard_constraint():
    """v022 及以后的每个版本都必须保留 causes 硬约束（防再次分叉丢失）。"""
    versions = _versions_at_least(22)
    assert versions, "未找到 v022 及以后的 l1_extraction 版本"
    for item in versions:
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        for anchor in CAUSES_ANCHORS:
            assert anchor in text, f"{item['version']} 丢失 causes 硬约束锚点：{anchor}"
