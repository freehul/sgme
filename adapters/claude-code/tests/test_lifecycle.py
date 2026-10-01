# -*- coding: utf-8 -*-
"""生命周期桥：增量导出 / noop 幂等 / 转录重写重放 / 追加失败不推进游标 / 启动容错。"""
from claude_sgme.lifecycle import JsonCursorStore, SessionBridge


class FakeAdapter:
    def __init__(self, sink=None, fail_append=False, fail_start=False):
        self.sink = sink if sink is not None else []
        self.fail_append = fail_append
        self.fail_start = fail_start
        self.settings = type("Settings", (), {"agent_id": "claude-code"})()

    def append(self, **kwargs):
        if self.fail_append:
            raise RuntimeError("append boom")
        self.sink.append(kwargs)
        return {"status": "new"}

    def mcp_tool(self, name, **kwargs):
        if self.fail_start:
            raise RuntimeError("mcp boom")
        return {"tool": name, "arguments": kwargs}

    def inject(self, **kwargs):
        if self.fail_start:
            raise RuntimeError("inject boom")
        return {"content": [{"type": "text", "text": "daily profile"}]}


def _msg(role, content, uuid, timestamp="2026-10-01T00:00:00Z"):
    return {"role": role, "content": content, "uuid": uuid, "timestamp": timestamp}


def _bridge(tmp_path, key="claude-s1", **kwargs):
    return SessionBridge(
        FakeAdapter(**kwargs),
        session_key=key,
        cursor_store=JsonCursorStore(tmp_path / "cursor.json"),
    )


def test_cursor_incremental_and_noop(tmp_path):
    sink = []
    bridge = _bridge(tmp_path, sink=sink)
    messages = [_msg("user", "第一问", "u1"), _msg("assistant", "第一答", "a1")]
    first = bridge.append_turn(messages)
    assert first["status"] == "new" and first["appended_messages"] == 2
    assert len(sink) == 1
    again = bridge.append_turn(messages)
    assert again["status"] == "noop" and len(sink) == 1
    messages.append(_msg("user", "第二问", "u2"))
    third = bridge.append_turn(messages)
    assert third["appended_messages"] == 1
    assert "第二问" in sink[-1]["content"] and "第一问" not in sink[-1]["content"]
    assert sink[-1]["session_key"] == "claude-s1"
    assert sink[-1]["agent_id"] == "claude-code"
    assert sink[-1]["content"].splitlines()[0].startswith("# ")


def test_cursor_replays_when_transcript_rewritten(tmp_path):
    sink = []
    bridge = _bridge(tmp_path, key="claude-s2", sink=sink)
    bridge.append_turn([_msg("user", "原文", "u1"), _msg("assistant", "原答", "a1")])
    # 转录被整体重写（压缩/迁移）：锚点对不上 → 全量重放（服务端按内容幂等兜底）
    rewritten = [_msg("user", "重写后的问题", "u9"), _msg("assistant", "重写后的回答", "a9")]
    result = bridge.append_turn(rewritten)
    assert result["appended_messages"] == 2


def test_cursor_replay_detected_without_uuid(tmp_path):
    """无 uuid 的转录用内容指纹做锚点——末条内容变了（重写）照样触发重放。"""
    sink = []
    bridge = _bridge(tmp_path, key="claude-s3", sink=sink)
    bridge.append_turn([_msg("user", "甲", ""), _msg("assistant", "乙", "")])
    changed = [_msg("user", "甲", ""), _msg("assistant", "乙（重写）", "")]
    result = bridge.append_turn(changed)
    assert result["appended_messages"] == 2


def test_append_failure_does_not_advance_cursor(tmp_path):
    cursor = JsonCursorStore(tmp_path / "cursor.json")
    messages = [_msg("user", "问", "u1"), _msg("assistant", "答", "a1")]
    broken = SessionBridge(FakeAdapter(fail_append=True), session_key="claude-s4", cursor_store=cursor)
    try:
        broken.append_turn(messages)
        raise AssertionError("should raise")
    except RuntimeError:
        pass
    assert cursor.get("claude-s4") == {}
    # 修复后重跑：全量补上（不是只补"缺口"，因为游标没推进）
    sink = []
    healthy = SessionBridge(FakeAdapter(sink=sink), session_key="claude-s4", cursor_store=cursor)
    result = healthy.append_turn(messages)
    assert result["appended_messages"] == 2


def test_started_at_is_monotonic(tmp_path):
    sink = []
    bridge = _bridge(tmp_path, key="claude-s5", sink=sink)
    bridge.append_turn([_msg("user", "一", "u1")])
    bridge.append_turn([_msg("user", "一", "u1"), _msg("user", "二", "u2")])
    assert sink[0]["started_at"] < sink[1]["started_at"]


def test_start_tolerates_partial_failures(tmp_path):
    bridge = _bridge(tmp_path, key="claude-s6", fail_start=True)
    result = bridge.start()
    assert "inject_error" in result and "signals_error" in result
