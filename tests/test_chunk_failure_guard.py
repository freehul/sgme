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
    memories, _provider, meta = l1.extract_l1(
        ["块一", "块二"], DIMS, LLM_CFG, chunk_retry_times=0)
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
        l1.extract_l1(["块一", "块二"], DIMS, LLM_CFG, chunk_retry_times=0)


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


# ---------- 集成：refine_one / refine_many 真的不推游标 ----------

def _fake_result(file_id, failed, total):
    r = RefineResult(file_id=file_id)
    r.failed_chunks, r.chunk_total = failed, total
    return r


def _patch_pipeline(monkeypatch, failed, total):
    """替换 refine_file / persist / commit，返回 commit 调用计数。"""
    from sgme.engine import refine as refine_mod

    calls = {"commit": 0, "persist": 0}

    def fake_refine_file(file_id, *a, **k):
        return _fake_result(file_id, failed, total)

    monkeypatch.setattr(refine_mod, "refine_file", fake_refine_file)
    monkeypatch.setattr(refine_mod, "commit_refine",
                        lambda *a, **k: calls.__setitem__("commit", calls["commit"] + 1))
    monkeypatch.setattr(pipeline, "persist_memories",
                        lambda *a, **k: calls.__setitem__("persist", calls["persist"] + 1) or dict(pipeline._ZERO_STATS))
    monkeypatch.setattr(pipeline, "_resolve_file_agent", lambda *a, **k: None)
    return calls


def test_refine_one_skips_commit_when_chunks_failed(monkeypatch):
    """有块失败 → 不推游标（文件保持 new），状态标 partial。"""
    calls = _patch_pipeline(monkeypatch, failed=2, total=10)
    result, _stats = pipeline.refine_one("f1", None, None, {})
    assert calls["commit"] == 0
    assert result.status == "partial"


def test_refine_one_commits_when_clean(monkeypatch):
    calls = _patch_pipeline(monkeypatch, failed=0, total=10)
    result, _stats = pipeline.refine_one("f1", None, None, {})
    assert calls["commit"] == 1
    assert result.status == "refined"


def test_refine_one_honours_ratio_config(monkeypatch):
    """配置容忍 5% 时，10 块里坏 1 块可提交。"""
    calls = _patch_pipeline(monkeypatch, failed=1, total=10)
    pipeline.refine_one("f1", None, None, {"refine": {"max_chunk_failure_ratio": 0.05}})
    assert calls["commit"] == 0          # 10% > 5%，拦住
    calls2 = _patch_pipeline(monkeypatch, failed=1, total=100)
    pipeline.refine_one("f1", None, None, {"refine": {"max_chunk_failure_ratio": 0.05}})
    assert calls2["commit"] == 1         # 1% ≤ 5%，放行


def test_refine_many_skips_commit_for_failed_file(monkeypatch):
    from sgme.engine import refine as refine_mod

    calls = _patch_pipeline(monkeypatch, failed=3, total=10)
    monkeypatch.setattr(refine_mod, "refine_batch",
                        lambda *a, **k: [_fake_result("f1", 3, 10), _fake_result("f2", 0, 10)])
    pairs = pipeline.refine_many(10, None, None, {})
    assert [r.status for r, _ in pairs] == ["partial", "refined"]
    assert calls["commit"] == 1          # 只有干净的那个推游标




# ---------- 块级重试（2026-11-03） ----------

def test_extract_l1_retries_transient_failure(monkeypatch):
    """瞬时失败（限流）在同轮内重试救回：不计失败块、退避按配置生效。"""
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise l1.RefineError("模拟限流")
        return [GOOD_MEM], "agnes", {"stage": "l1_extraction", "version": "v019", "variant": None}

    monkeypatch.setattr(l1, "_extract_l1_chunk", fake_chunk)
    monkeypatch.setattr(l1.time, "sleep", lambda s: sleeps.append(s))

    memories, _, meta = l1.extract_l1(
        ["块一", "块二"], DIMS, LLM_CFG, chunk_retry_times=2, chunk_retry_backoff_s=5.0)

    assert meta["chunk_failures"] == 0     # 救回来了，不算缺口
    assert meta["chunk_total"] == 2
    assert len(memories) == 1              # 两块同内容，去重后 1 条
    assert sleeps == [5.0]                 # 第 1 次失败 → 退避 5s
    assert calls["n"] == 3                 # 块一(失败+重试) + 块二


def test_extract_l1_gives_up_after_retries(monkeypatch):
    """重试耗尽才计入失败块，调用次数 = 1 + retry_times。"""
    calls = {"n": 0}

    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        calls["n"] += 1
        raise l1.RefineError("永久失败")

    monkeypatch.setattr(l1, "_extract_l1_chunk", fake_chunk)
    monkeypatch.setattr(l1.time, "sleep", lambda s: None)

    with pytest.raises(l1.RefineError):
        l1.extract_l1(["块一"], DIMS, LLM_CFG, chunk_retry_times=2)
    assert calls["n"] == 3


def test_retry_backoff_grows_linearly(monkeypatch):
    """退避递增：第 1 次 5s、第 2 次 10s（backoff × 第几次）。"""
    sleeps: list[float] = []

    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        raise l1.RefineError("一直失败")

    monkeypatch.setattr(l1, "_extract_l1_chunk", fake_chunk)
    monkeypatch.setattr(l1.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(l1.RefineError):
        l1.extract_l1(["块一"], DIMS, LLM_CFG, chunk_retry_times=2, chunk_retry_backoff_s=5.0)
    assert sleeps == [5.0, 10.0]


def test_retry_disabled_by_config(monkeypatch):
    calls = {"n": 0}

    def fake_chunk(chunk, dimensions, llm_cfg, **kwargs):
        calls["n"] += 1
        raise l1.RefineError("失败")

    monkeypatch.setattr(l1, "_extract_l1_chunk", fake_chunk)
    monkeypatch.setattr(l1.time, "sleep", lambda s: None)

    with pytest.raises(l1.RefineError):
        l1.extract_l1(["块一"], DIMS, LLM_CFG, chunk_retry_times=0)
    assert calls["n"] == 1


def test_default_config_has_chunk_retry():
    from sgme.config import DEFAULT_REFINE_CONFIG

    assert DEFAULT_REFINE_CONFIG["chunk_retry_times"] == 2
    assert DEFAULT_REFINE_CONFIG["chunk_retry_backoff_s"] == 5.0
