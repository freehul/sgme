"""ST-58/T-291：L1.5 降级直存补 `anomaly_warn`（P9）。

背景：降级直存意味着 **L1.5 冲突检测被整体跳过**——重复/冲突记忆直接落池，
且事后没有补检入口。生产 62 天内发生 **937 次且全部静默**，本条补上告警信号。

与 `refine.py` 的 deepseek 告警同型：**告警是旁路**，发布失败不得改变主流程结果。
"""

from __future__ import annotations

from sgme.engine import pipeline


class _Recorder:
    """替换 sgme.signal.engine.publish 的探针。"""

    def __init__(self, raise_on_call: bool = False):
        self.calls: list[tuple] = []
        self.raise_on_call = raise_on_call

    def __call__(self, event_type, source, payload, mem_conn):
        self.calls.append((event_type, source, dict(payload), mem_conn))
        if self.raise_on_call:
            raise RuntimeError("signal engine 不可用（测试模拟）")


def test_publish_payload_shape(monkeypatch):
    """事件类型/来源/负载字段符合约定（便于事后按 file_id 定位补检范围）。"""
    rec = _Recorder()
    monkeypatch.setattr("sgme.signal.engine.publish", rec)

    pipeline._publish_fallback_warn(
        mem_conn=None, file_id="f-0001", reason="l15_error: boom",
        memories_count=7,
    )

    assert len(rec.calls) == 1
    event_type, source, payload, _conn = rec.calls[0]
    assert event_type == "anomaly_warn"
    assert source == "l15_fallback"
    assert payload["file_id"] == "f-0001"
    assert "boom" in payload["reason"]
    assert payload["memories_count"] == 7


def test_publish_failure_is_nonblocking(monkeypatch):
    """告警发布失败不得影响降级直存结果（旁路纪律）。"""
    rec = _Recorder(raise_on_call=True)
    monkeypatch.setattr("sgme.signal.engine.publish", rec)

    # 不得抛出
    pipeline._publish_fallback_warn(
        mem_conn=None, file_id="f-0002", reason="l15_exception: x", memories_count=1
    )
    assert len(rec.calls) == 1


def test_missing_signal_engine_is_nonblocking(monkeypatch):
    """signal engine 未导入（模块不存在）时同样不炸。"""

    def _boom(*args, **kwargs):
        raise ImportError("no module named sgme.signal.engine")

    monkeypatch.setattr("sgme.signal.engine.publish", _boom)
    pipeline._publish_fallback_warn(
        mem_conn=None, file_id="f-0003", reason="x", memories_count=0
    )
