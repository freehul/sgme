import json

import pytest

from codex_sgme.mcp_client import McpClient, McpError


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeResponse:
    def __init__(self, payload, session_id=None):
        self.status_code = 200
        self.headers = FakeHeaders({"Mcp-Session-Id": session_id} if session_id else {})
        self.text = "event: message\ndata: " + json.dumps(payload) + "\n\n"


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        body = kwargs["json"]
        self.calls.append((url, kwargs))
        if body["method"] == "initialize":
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"serverInfo": {"name": "SGME"}},
                },
                session_id="session-1",
            )
        return FakeResponse(
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            }
        )


class ErrResponse:
    """非 2xx 响应（如 404 Session not found）。"""

    def __init__(self, status_code, text):
        self.status_code = status_code
        self.headers = FakeHeaders({})
        self.text = text


class ScriptedSession:
    """按预设响应序列依次返回的假会话（记录每次请求）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_mcp_client_initializes_once_and_calls_tool():
    session = FakeSession()
    client = McpClient(
        "http://10.0.0.10:9913/mcp",
        "agent-secret",
        session=session,
    )

    result = client.call_tool("agent_onboarding", {})

    assert result["content"][0]["text"] == "ok"
    assert len(session.calls) == 2
    assert session.calls[1][1]["headers"]["Mcp-Session-Id"] == "session-1"


def test_call_tool_reinitializes_on_stale_session():
    """服务端重启导致会话失效（404 Session not found）→ 重初始化并重试一次（T-240）。"""
    stale = ErrResponse(
        404,
        '{"jsonrpc":"2.0","id":"server-error","error":'
        '{"code":-32600,"message":"Session not found"}}',
    )
    init = FakeResponse(
        {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "SGME"}}},
        session_id="session-2",
    )
    ok = FakeResponse(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }
    )
    session = ScriptedSession([stale, init, ok])
    client = McpClient("http://10.0.0.10:9913/mcp", "agent-secret", session=session)
    client.session_id = "session-1"  # 模拟已有旧会话

    result = client.call_tool("agent_onboarding", {})

    assert result["content"][0]["text"] == "ok"
    assert len(session.calls) == 3
    assert session.calls[0][1]["headers"].get("Mcp-Session-Id") == "session-1"
    assert session.calls[2][1]["headers"].get("Mcp-Session-Id") == "session-2"


def test_non_session_404_is_not_retried():
    """非会话失效的 404 直接报错、不重试（防误重放有副作用的工具调用）。"""
    session = ScriptedSession([ErrResponse(404, '{"detail":"Not Found"}')])
    client = McpClient("http://10.0.0.10:9913/mcp", "agent-secret", session=session)
    client.session_id = "session-1"

    with pytest.raises(McpError):
        client.call_tool("append", {})

    assert len(session.calls) == 1
