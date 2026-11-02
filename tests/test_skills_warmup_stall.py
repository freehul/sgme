# -*- coding: utf-8 -*-
"""T-306 连带修复：向量预热循环「连续 3 轮零嵌入 → 停」。

事故形态（2026-10-29 全量测试）：publish 再生 skills/ 副本后 16 条技能标记待
重嵌入，而嵌入后端（Ollama bge-m3）未启动 → sync_index 静默返回
embedded=0/pending=16，旧循环无「无进展」退出，空转到 499 轮上限、每秒刷一行
stdout（约 8 分钟），串进其它用例的 capsys（test_care_consumer::
test_no_signals_silent 因此翻红）。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from sgme.server.app import _vector_warmup  # noqa: E402


class _Result:
    def __init__(self, data):
        self.data = data


class _Ops:
    """假 skills.sync_index：按 seq 出结果（耗尽后恒返 embedded=0/pending=16），计数调用。"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def sync_index(self, cfg, conn, wiki, max_embed=10, **kwargs):
        """签名要能吞下调用方新加的关键字参数（如 strict_gate / fts_check / rebuild_fts）。

        2026-11-02：sync_index 新增 strict_gate（批量容错）后本 mock 未同步，
        导致预热线程第一轮就 TypeError 退出、四处用例全红。用 **kwargs 兜底，
        避免以后再加参数时重演。
        """
        self.calls += 1
        item = self.seq.pop(0) if self.seq else {"embedded": 0, "pending_embed": 16}
        if isinstance(item, Exception):
            raise item
        return _Result(item)


def _run(seq):
    ops = _Ops(seq)
    rounds = _vector_warmup(ops, {}, None, None, sleep=lambda s: None)
    return ops, rounds


def test_stall_breaks_after_three_zero_rounds(capsys):
    """后端不可用（恒 embedded=0）→ 3 轮即停，只打一条暂停告知，不逐轮刷屏。"""
    ops, rounds = _run([])
    assert ops.calls == 3
    assert rounds == 3
    out = capsys.readouterr().out
    assert "连续 3 轮零嵌入" in out
    assert "向量预热第" not in out  # 零嵌入轮不再打轮次行


def test_progress_resets_stall(capsys):
    """中间有成功嵌入 → 重置连败计数，第 5 轮才暂停；进展轮照常打轮次行。"""
    ops, _ = _run([
        {"embedded": 0, "pending_embed": 16},
        {"embedded": 10, "pending_embed": 6},
        {"embedded": 0, "pending_embed": 6},
        {"embedded": 0, "pending_embed": 6},
        {"embedded": 0, "pending_embed": 6},
    ])
    assert ops.calls == 5
    out = capsys.readouterr().out
    assert "向量预热第 2 轮：嵌入 10 条" in out
    assert "连续 3 轮零嵌入" in out


def test_done_exits_clean(capsys):
    """pending 归零 → 静默结束；有嵌入且归零 → 打一条轮次行后结束。"""
    ops, _ = _run([{"embedded": 0, "pending_embed": 0}])
    assert ops.calls == 1
    assert capsys.readouterr().out == ""

    ops, _ = _run([{"embedded": 10, "pending_embed": 0}])
    assert ops.calls == 1
    assert "向量预热第 1 轮：嵌入 10 条" in capsys.readouterr().out


def test_exception_breaks(capsys):
    """sync_index 抛异常 → 打中断并退出（保持原语义）。"""
    ops, _ = _run([RuntimeError("db locked")])
    assert ops.calls == 1
    assert "向量预热中断" in capsys.readouterr().out
