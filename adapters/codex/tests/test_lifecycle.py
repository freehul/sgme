from codex_sgme.lifecycle import JsonCursorStore, SessionBridge


class FakeAdapter:
    def __init__(self, *, fail_append=False):
        self.fail_append = fail_append
        self.append_calls = []
        self.mcp_calls = []

    def inject(self, **kwargs):
        return {"mode": kwargs["mode"]}

    def append(self, **kwargs):
        if self.fail_append:
            raise RuntimeError("append failed")
        self.append_calls.append(kwargs)
        return {"status": "new", "file_id": "file-1"}

    def mcp_tool(self, name, **arguments):
        self.mcp_calls.append((name, arguments))
        if name == "signal_pull":
            return {"signals": []}
        return {"status": "queued"}


def test_start_injects_profile_and_pulls_signals(tmp_path):
    adapter = FakeAdapter()
    bridge = SessionBridge(
        adapter,
        session_key="codex-test",
        cursor_store=JsonCursorStore(tmp_path / "cursor.json"),
    )

    result = bridge.start()

    assert result == {"inject": {"mode": "daily"}, "signals": {"signals": []}}
    assert adapter.mcp_calls == [("signal_pull", {"limit": 20})]


def test_append_turn_filters_non_conversation_messages_and_advances_cursor(tmp_path):
    adapter = FakeAdapter()
    bridge = SessionBridge(
        adapter,
        session_key="codex-test",
        cursor_store=JsonCursorStore(tmp_path / "cursor.json"),
    )
    messages = [
        {"role": "user", "content": "hello", "timestamp": "2026-09-26T10:00:00Z"},
        {"role": "system", "content": "do not store"},
        {"role": "tool", "content": "tool receipt"},
        {
            "role": "assistant",
            "content": "world",
            "timestamp": "2026-09-26T10:00:01Z",
        },
    ]

    bridge.append_turn(messages, started_at="2026-09-26T10:00:00.000Z")
    bridge.append_turn(messages, started_at="2026-09-26T10:00:00.000Z")

    assert len(adapter.append_calls) == 2
    first, second = adapter.append_calls
    assert first["agent_id"] == "codex"
    assert first["content"] == (
        "# 2026-09-26T10:00:00Z user\nhello\n\n"
        "## 2026-09-26T10:00:01Z assistant\nworld"
    )
    assert "do not store" not in first["content"]
    assert "tool receipt" not in first["content"]
    assert second["started_at"] > first["started_at"]


def test_failed_append_does_not_advance_cursor(tmp_path):
    store = JsonCursorStore(tmp_path / "cursor.json")
    bridge = SessionBridge(
        FakeAdapter(fail_append=True),
        session_key="codex-test",
        cursor_store=store,
    )

    try:
        bridge.append_turn(
            [{"role": "user", "content": "hello"}],
            started_at="2026-09-26T10:00:00.000Z",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected append failure")

    assert store.get("codex-test") == {}


def test_end_triggers_async_refine(tmp_path):
    adapter = FakeAdapter()
    bridge = SessionBridge(
        adapter,
        session_key="codex-test",
        cursor_store=JsonCursorStore(tmp_path / "cursor.json"),
    )

    assert bridge.end() == {"status": "queued"}
    assert adapter.mcp_calls == [("refine_trigger", {"async_mode": True})]


def test_cursor_store_persists_atomically(tmp_path):
    path = tmp_path / "cursor.json"
    store = JsonCursorStore(path)

    store.update("codex-test", {"last_started_at": "2026-09-26T10:00:00.000Z"})

    assert JsonCursorStore(path).get("codex-test")["last_started_at"] == (
        "2026-09-26T10:00:00.000Z"
    )
    assert not list(tmp_path.glob("*.tmp"))
