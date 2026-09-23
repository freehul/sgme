"""tests/test_refine_resilience.py：提炼韧性（F-2/F-5，2026-09-24 深度审查）。

覆盖：
1. refine_batch 逐文件容错：单文件异常 → 收成 status=error 项并继续，其余文件正常
2. refine_many 逐文件立即落库：第 1 个文件已落库，第 2 个文件异常不影响它
   （原缺陷：整批集齐再落库，中途异常 → 前序文件已标 refined 但记忆永久丢失）
3. async_refine_worker 批量分支透传 agent_tag（F-5：多 Agent 隔离打标缺口）

全部用替身桩 + 内存连接占位，不真调 LLM、不碰真实 DB。
"""
from __future__ import annotations

import sqlite3

from sgme.engine import pipeline as pipeline_mod
from sgme.engine import refine as refine_mod

# 连接占位（全部 DB 调用被桩替换，仅满足类型签名，不产生任何读写）
_CONN = sqlite3.connect(":memory:")


def _stub_scan(monkeypatch, file_ids):
    """把 refine.session_dao.list_by_status 替换为固定文件列表。"""
    monkeypatch.setattr(
        refine_mod.session_dao, "list_by_status",
        lambda conn, status, limit=100: [{"file_id": fid} for fid in file_ids[:limit]],
    )


def _stub_refine_file(monkeypatch, fail_on=None, memories_per_file=False):
    """替换 refine.refine_file：fail_on 文件抛异常，其余返回 RefineResult。"""

    def fake(file_id, mem_conn, session_conn, cfg, client=None, source_type="session"):
        if fail_on is not None and file_id == fail_on:
            raise RuntimeError("Model is unloaded")
        r = refine_mod.RefineResult(file_id=file_id)
        if memories_per_file:
            r.memories = [{"content": f"mem-{file_id}"}]
        return r

    monkeypatch.setattr(refine_mod, "refine_file", fake)


def _stub_persist(monkeypatch, collector):
    """替换 pipeline.persist_memories：记录 (file_id, agent_tag) 并返回零值统计。"""
    monkeypatch.setattr(
        pipeline_mod, "persist_memories",
        lambda r, mc, c, agent_tag=None: (
            collector.append((r.file_id, agent_tag)) or dict(pipeline_mod._ZERO_STATS)
        ),
    )


# ---------- 1. refine_batch 逐文件容错 ----------


def test_refine_batch_tolerates_per_file_exception(monkeypatch):
    """单文件异常不再中断批次：error 项 + 其余文件照常产出。"""
    _stub_scan(monkeypatch, ["f1", "f2", "f3"])
    _stub_refine_file(monkeypatch, fail_on="f2")

    results = list(refine_mod.refine_batch(_CONN, _CONN, {}, limit=10))

    assert [r.file_id for r in results] == ["f1", "f2", "f3"]
    assert [r.status for r in results] == ["refined", "error", "refined"]
    assert "Model is unloaded" in (results[1].error or "")


def test_refine_batch_is_lazy_generator(monkeypatch):
    """生成器语义：调用不消费，迭代才逐文件产出（调用方据此逐文件即时落库）。"""
    import inspect

    assert inspect.isgeneratorfunction(refine_mod.refine_batch)
    _stub_scan(monkeypatch, ["f1", "f2"])
    calls: list[str] = []
    monkeypatch.setattr(
        refine_mod, "refine_file",
        lambda file_id, *a, **kw: (calls.append(file_id) or refine_mod.RefineResult(file_id=file_id)),
    )

    gen = refine_mod.refine_batch(_CONN, _CONN, {}, limit=10)
    assert calls == []  # 未迭代 → 未提炼
    first = next(gen)
    assert first.file_id == "f1"
    assert calls == ["f1"]  # 逐文件惰性产出


# ---------- 2. refine_many 逐文件立即落库 ----------


def test_refine_many_persists_per_file_before_failure(monkeypatch):
    """第 2 个文件异常时，第 1 个文件的记忆已立即落库（原缺陷：整批 0 落库）。"""
    _stub_scan(monkeypatch, ["f1", "f2"])
    _stub_refine_file(monkeypatch, fail_on="f2", memories_per_file=True)
    persisted: list[tuple[str, str | None]] = []
    _stub_persist(monkeypatch, persisted)
    monkeypatch.setattr(pipeline_mod, "_resolve_file_agent", lambda conn, fid: "probe-agent")

    pairs = pipeline_mod.refine_many(limit=10, mem_conn=_CONN, session_conn=_CONN, cfg={})

    # 关键断言：f1 在 f2 抛异常之前已完成落库
    assert persisted == [("f1", "probe-agent")]
    assert [p[0].file_id for p in pairs] == ["f1", "f2"]
    assert pairs[1][0].status == "error"
    assert pairs[1][1] == dict(pipeline_mod._ZERO_STATS)  # error 项无 memories → 零值统计


def test_refine_many_passes_agent_tag(monkeypatch):
    """同步批量路径 agent_tag 逐文件解析透传（与异步/单文件路径一致）。"""
    _stub_scan(monkeypatch, ["f1", "f2"])
    _stub_refine_file(monkeypatch, memories_per_file=True)
    seen: list[tuple[str, str | None]] = []
    _stub_persist(monkeypatch, seen)
    monkeypatch.setattr(pipeline_mod, "_resolve_file_agent", lambda conn, fid: "agt-probe")

    pipeline_mod.refine_many(limit=10, mem_conn=_CONN, session_conn=_CONN, cfg={})

    assert seen == [("f1", "agt-probe"), ("f2", "agt-probe")]


# ---------- 3. async_refine_worker 批量分支 agent_tag（F-5） ----------


def test_async_worker_batch_passes_agent_tag(monkeypatch):
    """异步批量分支此前漏传 agent_tag → 现补齐（T-140 多 Agent 打标）。"""
    _stub_scan(monkeypatch, ["f1", "f2"])
    _stub_refine_file(monkeypatch, memories_per_file=True)
    seen: list[tuple[str, str | None]] = []
    _stub_persist(monkeypatch, seen)
    monkeypatch.setattr(pipeline_mod, "_resolve_file_agent", lambda conn, fid: "agt-probe")

    pipeline_mod.async_refine_worker(None, 10, _CONN, _CONN, {})

    assert seen == [("f1", "agt-probe"), ("f2", "agt-probe")]
