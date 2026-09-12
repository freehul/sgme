"""批量向量重试/拆批测试（2026-09-13 单题失败复盘驱动）。

背景：0100672e 在评测里连续两次整题作废——向量端点瞬时拒连（WinError 10061）让
32 条整批连续 6 次失败直接抛错。加固：失败重试后仍失败且批 >1 条 → 二分拆批递归，
小批更容易穿过抖动，单条超限也不再拖垮整批。
"""
from __future__ import annotations

from eval import longmemeval_eval as L


class _Resp:
    def __init__(self, ok: bool, n: int = 0):
        self.status_code = 200 if ok else 500
        self._ok = ok
        self._n = n

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("boom 500")

    def json(self):
        return {"data": [{"index": i, "embedding": [0.0, 1.0]}
                         for i in range(self._n)]}


class _Cli:
    """>max_ok 条输入一律失败，≤max_ok 条成功（模拟「大批过不去、小批能过」）。"""

    def __init__(self, max_ok: int = 1):
        self.max_ok = max_ok
        self.calls: list[int] = []

    def post(self, url, json=None):
        n = len(json["input"])
        self.calls.append(n)
        return _Resp(ok=(n <= self.max_ok), n=n if n <= self.max_ok else 0)


def test_splits_batch_when_every_attempt_fails():
    cli = _Cli(max_ok=1)
    batch = [(0, "m1", "t1"), (1, "m2", "t2")]
    out = L._embed_with_split(cli, "http://x/v1", "model", batch, max_attempts=2, sleep_s=0.0)
    assert [o[0] for o in out] == ["m1", "m2"]
    assert [o[1] for o in out] == ["t1", "t2"]
    assert len(out) == 2
    assert max(cli.calls) == 2, "应先试整批、失败后拆批"
    assert min(cli.calls) == 1, "拆批后应为单条"


def test_succeeds_without_split_when_batch_fits():
    cli = _Cli(max_ok=8)
    batch = [(0, "a", "x"), (1, "b", "y")]
    out = L._embed_with_split(cli, "http://x/v1", "model", batch, max_attempts=2, sleep_s=0.0)
    assert len(out) == 2
    assert cli.calls == [2], "整批一次成功，不应拆批"


def test_single_item_exhaustion_still_raises():
    import pytest

    cli = _Cli(max_ok=0)
    with pytest.raises(RuntimeError, match="耗尽重试"):
        L._embed_with_split(cli, "http://x/v1", "model", [(0, "m", "t")], max_attempts=2, sleep_s=0.0)
