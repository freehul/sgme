"""L1 提炼提示词 2.0 结构契约（防「结构硬约束」再次丢失）。

背景（2026-11-07 缺陷）：v014 给 2.0 提示词加了「结构是硬性的」防回退约束——模型若
输出 1.x 平铺数组，`engine/l2_extract.py` 会静默委托旧解析，2.0 字段（facts/causes/
trait/dimensions/route/subject…）全部作废且不报错。但 v015 误从 v013 分叉，v014 的这
段内容在 v015~v019 一路缺失，登录版本 v019 也缺。

本测试对「v020 及以后」的每一个 l1_extraction 版本文件做契约断言：谁再丢掉这段约束，
测试立即红。历史版本（v001~v019）不在断言范围内，不改动。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "sgme" / "resources" / "prompts"
MANIFEST = PROMPTS_ROOT / "manifest.yaml"

# 结构硬约束的三个锚点（缺任一条即视为约束丢失）
HARD_ANCHORS = (
    "结构是硬性的，不是建议",
    "禁止**输出平铺数组",
    "自检：输出前逐个元素确认有 `main_event` / `dimensions` / `route` 三个键",
)
# 输出要求末尾的第 8 条
TAIL_ANCHOR = "禁止平铺数组——结构错了，2.0 字段全部作废"
# 过期状态行（v019 及更早的草案残留；v020 起禁止出现）
STALE_STATUS = "状态：草案，未登记 manifest"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _versions_at_least(minimum: int) -> list[dict]:
    """返回版本号 >= minimum 的 l1_extraction 版本条目。"""
    versions = _manifest()["versions"]["l1_extraction"]
    out = []
    for item in versions:
        tag = item["version"]
        assert tag.startswith("v") and tag[1:].isdigit(), f"非法版本号 {tag}"
        if int(tag[1:]) >= minimum:
            out.append(item)
    return out


def test_v020_and_later_keep_structural_hard_constraint():
    """v020 起每个版本都必须含 v014 的结构硬约束（正文 + 输出要求）。"""
    versions = _versions_at_least(20)
    assert versions, "未找到 v020 及以后的 l1_extraction 版本"
    for item in versions:
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        for anchor in HARD_ANCHORS:
            assert anchor in text, f"{item['version']} 丢失结构硬约束锚点：{anchor}"
        assert TAIL_ANCHOR in text, f"{item['version']} 丢失输出要求第 8 条"


def test_v020_and_later_no_stale_draft_status():
    """v020 起不得再出现「草案、未激活」这类过期状态行。"""
    for item in _versions_at_least(20):
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        assert STALE_STATUS not in text, f"{item['version']} 仍含过期状态行"


def test_v020_and_later_version_chain_mentions_v014():
    """头部版本链必须能溯源到 v014（分叉丢失的起点）。"""
    for item in _versions_at_least(20):
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        assert "# v014（2026-10-31）" in text, f"{item['version']} 版本链缺 v014"


def test_manifest_sha256_matches_v020_file():
    """manifest 登记的 v020 sha256 必须与文件一致（防手改文件后忘记重登记）。

    口径与 `PromptStore._lazy_verify_sha` 对齐：read_text 归一换行后按 UTF-8 计算文本 sha，
    避免 Windows autocrlf 把工作区转成 CRLF 时字节 sha 漂移造成误报。
    """
    for item in _versions_at_least(20):
        path = PROMPTS_ROOT / item["file"]
        digest = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        assert digest == item["sha256"], (
            f"{item['version']} sha256 不一致：manifest={item['sha256'][:12]} 实际={digest[:12]}"
        )


def test_working_copy_matches_active_or_latest():
    """工作副本必须与「最新登记版本」内容一致（防 @working 回退成旧提示词）。"""
    versions = _versions_at_least(20)
    latest = max(versions, key=lambda v: int(v["version"][1:]))
    latest_text = (PROMPTS_ROOT / latest["file"]).read_text(encoding="utf-8")
    working_text = (PROMPTS_ROOT / "l1_extraction.txt").read_text(encoding="utf-8")
    assert working_text == latest_text, "工作副本与最新登记版本不一致"
