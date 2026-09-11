"""LM Studio 端点守护（scripts/lmstudio_watchdog.py）单元测试。

守护逻辑里唯一需要单测的是「哪些模型该补装」的判定——它决定了长跑期间
端点抖动恢复后能不能自动把缺失模型装回来（2026-09-11 实测服务端静默停止）。
"""

from __future__ import annotations

from scripts.lmstudio_watchdog import DEFAULT_KEEP, missing_models


def test_missing_models_reports_absent_ids():
    loaded = {"qwen3.8-9b-distill"}
    wanted = ["qwen3.8-9b-distill", "text-embedding-bge-m3-legal-euro-r7"]
    assert missing_models(loaded, wanted) == ["text-embedding-bge-m3-legal-euro-r7"]


def test_missing_models_empty_when_all_loaded():
    wanted = [k[0] for k in DEFAULT_KEEP]
    assert missing_models(set(wanted), wanted) == []


def test_default_keep_covers_refine_and_embed():
    ids = [k[0] for k in DEFAULT_KEEP]
    assert any("qwen" in i for i in ids), "必须守护提炼模型"
    assert any("embed" in i for i in ids), "必须守护向量模型"
    # 装载参数随守护一起固定（模型缺失时按该档位回装）
    assert any("--parallel" in args for _, args in DEFAULT_KEEP)
