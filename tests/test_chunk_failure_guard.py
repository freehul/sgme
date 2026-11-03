"""tests/test_chunk_failure_guard.py：块级失败不得静默标记 refed（2026-11-03 T-3xx）。

背景（2026-11-02 夜间全量提炼实测）：``l1.extract_l1`` 对单块失败只记 warning 后
``continue``，整份文件仍会 commit 并标 ``refined`` —— 失败不阻断，而是**静默缺内容**。
实测 ed7c89fd 在限流阶段失败 71 次 / 200 块 = 35% 的块没进，却照样标 refined。

修复口径：
1. ``extract_l1`` 把块级失败数写进 meta（``chunk_failures`` / ``chunk_total``）
2. ``refine_file`` 透传成 ``RefineResult.failed_chunks`` 并置 ``anomaly_warn``
3. 提交闸 ``pipeline.should_commit``：有失败块 → **不推进游标**（文件保持 status=new 待重跑）
"""
from __future__ import annotations

import pytest

from sgme.engine import l1, pipeline
from sgme.engine.refine import RefineResult

DIMS = [
    {"id": "status", "name": "状态", "type": "dynamic"},
    {"id": "identity", "name": "身份", "type": "static"},
]
LLM_CFG = {"chains": {"refinement": []}, "rules": {}}

GOOD_MEM = {
    "content": "用户在做显存调优",
    "memory_type": "fact",
    "priority": 50,
    "time_velocity": "static",
    "dimensions": ["status"],
}


def test_extract_l1_reports_chunk_failures(monkeypatch):
    """两块里挂一块 → meta 必须报 chunk_failures=1 / chunk_total=2。"""
    calls = {"n": 0}

    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise l1.RefineError("模拟单块失败")
        return [GOOD_MEM], "agnes", {"stage": "l1_extraction", "version": "v019", "variant": None}

    monkeypatch.setattr(l1, "_extract_l1_chunk", fake_chunk)
    memories, _provider, meta = l1.extract_l1(["块一", "块二"], DIMS, LLM_CFG)
    assert len(memories) == 1          # 成功块的记忆保留
    assert meta["chunk_failures"] == 1
    assert meta["chunk_total"] == 2


def test_extract_l1_all_ok_reports_zero(monkeypatch):
    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        return [GOOD_MEM], "agnes", {"stage": "l1_extraction", "version": "v019", "variant": None}

    monkeypatch.setattr(l1, "_extract_l1_chunk", fake_chunk)
    _, _, meta = l1.extract_l1(["块一", "块二"], DIMS, LLM_CFG)
    assert meta["chunk_failures"] == 0
    assert meta["chunk_total"] == 2


def test_extract_l1_all_failed_still_raises(monkeypatch):
    """全块失败仍按 RefineError 抛出（保持既有语义，不静默产空）。"""
    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        raise l1.RefineError("块失败")

    monkeypatch.setattr(l1, "_extract_l1_chunk", fake_chunk)
    with pytest.raises(l1.RefineError):
        l1.extract_l1(["块一", "块二"], DIMS, LLM_CFG)


# ---------- 提交闸 ----------

def test_should_commit_blocks_on_failed_chunks():
    r = RefineResult(file_id="f1")
    r.failed_chunks = 3
    assert pipeline.should_commit(r) is False


def test_should_commit_allows_clean_result():
    r = RefineResult(file_id="f1")
    assert pipeline.should_commit(r) is True


def test_should_commit_tolerates_old_result_without_field():
    """老调用方/老结果没有该字段时按干净处理（向后兼容）。"""

    class _Old:
        file_id = "f1"

    assert pipeline.should_commit(_Old()) is True


def test_should_commit_ratio_allows_within_threshold():
    """显式配置容忍度时：缺口在阈值内 → 允许提交（仍由 anomaly_warn 留痕）。"""
    r = RefineResult(file_id="f1")
    r.failed_chunks, r.chunk_total = 1, 20
    assert pipeline.should_commit(r, 0.05) is True


def test_should_commit_ratio_blocks_above_threshold():
    r = RefineResult(file_id="f1")
    r.failed_chunks, r.chunk_total = 5, 20
    assert pipeline.should_commit(r, 0.05) is False


def test_should_commit_zero_ratio_blocks_even_one_failure():
    r = RefineResult(file_id="f1")
    r.failed_chunks, r.chunk_total = 1, 500
    assert pipeline.should_commit(r, 0.0) is False


def test_default_config_is_zero_tolerance():
    from sgme.config import DEFAULT_REFINE_CONFIG

    assert DEFAULT_REFINE_CONFIG["max_chunk_failure_ratio"] == 0.0

