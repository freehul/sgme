"""v021 提示词契约：一次性 UI/操作反馈的提炼口径（W09，2026-11-08）。

背景
----
生产 40,718 条 active 记忆里，按钮/黑白相关 93 条，抽样 20 条中 11 条是裸事实
（"用户反馈提交按钮黑字看不清，要求改成白色"），只有 9 条带 l2_facts_json、更少的
带 causes。v021 在「什么不是记忆」增加第 4 类：一次性 UI/操作反馈本身不记；形成
可复用规则/标准时只记规则并写清「原因 → 结论」；项目设计决策/长期偏好按决策记录；
用户明确要求记住的例外照记。

本测试只锁 v021 的行为契约（不复用其他文件的断言集，避免与既有 v020 契约测试耦合）：
1. manifest active 指向 v021，且 v021 已登记、sha256 与文件一致；
2. 工作副本与 v021 逐字一致（防 @working 回退）；
3. v021 含第 4 类判据与 2 组正反例（裸事实 vs 规则/决策）；
4. v021 不新增占位符——提示词改动不得引入新的渲染变量/新 LLM 调用；
5. v022 及以后默认继承该口径（防再次分叉丢失，与 v020 结构硬约束同一模式）。
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

# 第 4 类判据 + 2 组正反例的锚点（缺任一条视为口径丢失）
UI_FEEDBACK_ANCHORS = (
    "一次性 UI/操作反馈",
    "本身不产出记忆",
    "原因 → 结论",
    "`facts`（`type=约束`）",
    "`causes`（`from` = 原因，`to` = 结论）",
    "项目设计决策 / 长期偏好",
    "用户**明确要求记住**",
    "❌ 裸事实：「用户反馈提交按钮黑字看不清，要求改成白色。」",
    "✅ 规则：「深色底黑字对比度不足 → 界面按钮统一白字配实色底，禁用低对比方案。」",
    "❌ 裸事实：「用户让把左栏改成上下两栏。」",
    "✅ 决策：「AIRDT 左栏采用上下两栏：上部项目操作/列表，下部导航。」",
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


def test_manifest_active_is_latest_registered():
    """active 指向最新登记版本（发布态约定；回滚应显式修改本断言并留痕）。

    v021 交付时此断言为 "v021"；v022 起改为「active == 最新版本」，
    避免每次新增版本都要改 v021 的交付测试（发布决策由 manifest diff 体现）。
    """
    active = _manifest()["stages"]["l1_extraction"]["active"]
    latest = max(_versions_at_least(21), key=lambda v: int(v["version"][1:]))["version"]
    assert active == latest, f"active={active}，期望最新登记版本 {latest}"


def test_v021_registered_and_sha_matches():
    """v021 已登记 manifest，且 sha256 与文件内容一致（防手改后忘记重登记）。"""
    entries = {v["version"]: v for v in _versions_at_least(21)}
    assert "v021" in entries, "manifest 未登记 v021"
    path = PROMPTS_ROOT / entries["v021"]["file"]
    assert path.exists(), f"v021 文件不存在: {path}"
    digest = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert digest == entries["v021"]["sha256"], (
        f"v021 sha256 不一致：manifest={entries['v021']['sha256'][:12]} 实际={digest[:12]}"
    )


def test_working_copy_matches_active_version():
    """工作副本必须与 manifest active 指向的版本逐字一致（否则 @working 会回退成旧口径）。

    v021 交付时 active=v021；v022 起 active 前移，本断言跟随 manifest，
    避免每加一个版本就改坏旧交付测试（v021 文件本身的锚点由下方参数化用例锁定）。
    """
    active = _manifest()["stages"]["l1_extraction"]["active"]
    assert active.startswith("v"), f"active 不是钉版: {active}"
    active_text = (PROMPTS_ROOT / "versions" / "l1_extraction" / f"{active}.txt").read_text(encoding="utf-8")
    working = (PROMPTS_ROOT / "l1_extraction.txt").read_text(encoding="utf-8")
    assert working == active_text, f"工作副本与 active [{active}] 不一致"


@pytest.mark.parametrize("anchor", UI_FEEDBACK_ANCHORS)
def test_v021_contains_ui_feedback_rule(anchor):
    """v021 必须含第 4 类判据与正反例锚点（逐条断言，坏在哪条一眼可见）。"""
    text = (VERSIONS_DIR / "v021.txt").read_text(encoding="utf-8")
    assert anchor in text, f"v021 缺少 UI/操作反馈口径锚点：{anchor}"


def test_v021_keeps_v020_structural_contract():
    """v021 不得丢失 v020 的结构硬约束与输出要求第 8 条。"""
    text = (VERSIONS_DIR / "v021.txt").read_text(encoding="utf-8")
    for anchor in (
        "结构是硬性的，不是建议",
        "禁止**输出平铺数组",
        "自检：输出前逐个元素确认有 `main_event` / `dimensions` / `route` 三个键",
        "禁止平铺数组——结构错了，2.0 字段全部作废",
    ):
        assert anchor in text, f"v021 丢失 v020 结构约束：{anchor}"


def test_v021_no_new_placeholders():
    """v021 不得新增占位符：占位符集合须与 v020 完全相同（防新渲染变量/新 LLM 调用）。"""

    def placeholders(text: str) -> set[str]:
        return set(re.findall(r"\{\{[a-z_]+\}\}", text))

    v020 = (VERSIONS_DIR / "v020.txt").read_text(encoding="utf-8")
    v021 = (VERSIONS_DIR / "v021.txt").read_text(encoding="utf-8")
    assert placeholders(v021) == placeholders(v020), (
        f"v021 占位符集合变化：{sorted(placeholders(v021) ^ placeholders(v020))}"
    )


def test_later_versions_inherit_ui_feedback_rule():
    """v021 及以后的每个版本都必须保留 UI/操作反馈口径（防再次分叉丢失）。"""
    versions = _versions_at_least(21)
    assert versions, "未找到 v021 及以后的 l1_extraction 版本"
    for item in versions:
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        for anchor in UI_FEEDBACK_ANCHORS:
            assert anchor in text, f"{item['version']} 丢失 UI/操作反馈锚点：{anchor}"
