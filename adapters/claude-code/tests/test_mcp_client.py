# -*- coding: utf-8 -*-
"""MCP 客户端：会话失效自愈（404 Session not found → 重初始化重试一次）与空响应边界。"""
import json

import pytest

from claude_sgme.mcp_client import McpClient, McpError


class _Resp:
    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _data_frame(request_id, result):
    return "data: " + json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


class ScriptedSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"headers": dict(headers or {}), "body": json})
        return self.script.pop(0)

    def close(self):
        pass


def test_stale_session_retries_once_after_reinitialize():
    session = ScriptedSession(
        [
            _Resp(200, _data_frame(1, {"ok": True}), {"Mcp-Session-Id": "s1"}),
            _Resp(404, '{"error": "Session not found"}'),
            _Resp(200, _data_frame(1, {"ok": True}), {"Mcp-Session-Id": "s2"}),
            _Resp(200, _data_frame(2, {"content": [{"type": "text", "text": "ok"}]})),
        ]
    )
    client = McpClient("http://x/mcp", "agt_test", session=session)
    result = client.call_tool("health", {})
    assert result["content"][0]["text"] == "ok"
    # 初始化 → 带 s1 调用（404）→ 重初始化 → 带 s2 重发
    assert len(session.calls) == 4
    assert session.calls[1]["headers"].get("Mcp-Session-Id") == "s1"
    assert session.calls[3]["headers"].get("Mcp-Session-Id") == "s2"


def test_empty_event_stream_raises_mcp_error():
    session = ScriptedSession([_Resp(200, "not-an-event-stream")])
    client = McpClient("http://x/mcp", "agt_test", session=session)
    with pytest.raises(McpError):
        client.initialize()


def test_missing_key_raises_before_network():
    session = ScriptedSession([])
    client = McpClient("http://x/mcp", None, session=session)
    with pytest.raises(ValueError):
        client.initialize()
    assert session.calls == []
