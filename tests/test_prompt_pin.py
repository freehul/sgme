"""提示词钉版（`BucketCtx.overrides`）——发布回退与 A/B 对照的基础设施。

背景（2026-10-17）：批次 1 的提示词 v005（+1 行「反斜杠必须转义」）是唯一会在部署后
**自动生效**的行为变更（manifest `active: @working`）。发布前评估时把它**摘出**本次发布，
做法是 `activate('l1_conflict','v004')` 把 active 钉回改前快照——而不是改写工作副本，
避免内容漂移。本文件锁住钉版语义，防止回退被后续操作静默覆盖。
"""

from __future__ import annotations

import io

import pytest
import yaml

from sgme.prompts import BucketCtx
from sgme.prompts.manager import PromptManifestError, PromptStore

STAGE = "l1_conflict"
#: v005 新增的那行约束（用于判别到底加载了哪一版）
V005_MARK = "反斜杠必须转义"


@pytest.fixture(scope="module")
def store():
    return PromptStore()


def test_pin_overrides_manifest(store):
    """钉 v004 / v005 必须分别拿到对应文本，且不受 manifest active 影响。"""
    v4 = store.get(STAGE, BucketCtx(bucket_key="k", overrides={STAGE: "v004"}))
    v5 = store.get(STAGE, BucketCtx(bucket_key="k", overrides={STAGE: "v005"}))
    assert v4.version == "v004"
    assert v5.version == "v005"
    assert V005_MARK not in v4.text
    assert V005_MARK in v5.text


def test_default_follows_manifest_active(store):
    """不传 overrides 时跟随 manifest 的 active（部署态行为）。"""
    manifest = yaml.safe_load(
        io.open(store.prompts_root / "manifest.yaml", encoding="utf-8"))
    active = manifest["stages"][STAGE]["active"]
    pv = store.get(STAGE, BucketCtx(bucket_key="k"))
    if active == "@working":
        assert pv.version.startswith("working-")
    else:
        assert pv.version == active


def test_rollback_state_is_pinned_to_v004(store):
    """**发布回退的状态锁**：2026-10-17 起本仓库 l1_conflict 钉在 v004。

    若有人把 active 改回 `@working`（工作副本是 v005）而未走验证流程，本用例会红——
    这是有意的：提示词改动需要单独过 3 次重复对照后才能启用（见 B257）。

    ⚠️ 该断言是**发布意图**，不是代码不变量。等 v005 通过验证、正式启用时，
    应同步修改本用例（这次改动会出现在 diff 里，成为一次显式的发布决策）。
    """
    manifest = yaml.safe_load(
        io.open(store.prompts_root / "manifest.yaml", encoding="utf-8"))
    assert manifest["stages"][STAGE]["active"] == "v004"


def test_rollback_text_equals_production_sha(store):
    """回退后的正文应与「生产当前在跑的内容」逐字节一致（sha 前缀 e6d9a33f）。

    依据：2026-10-17 实测生产自报 `l1_conflict: working-e6d9a33f`。
    → 部署后该 stage 行为与现状完全相同（这是「摘出提示词改动」的验证口径）。
    """
    import hashlib

    pv = store.get(STAGE, BucketCtx(bucket_key="k"))
    assert hashlib.sha256(pv.text.encode("utf-8")).hexdigest().startswith("e6d9a33f")


def test_pin_unknown_version_raises(store):
    """钉不存在的版本必须显式报错（不许静默回落到 working）。"""
    with pytest.raises(Exception):  # PromptManifestError 或 KeyError，均属显式失败
        store.get(STAGE, BucketCtx(bucket_key="k", overrides={STAGE: "v999"}))
