import json

from codex_sgme.mcp_client import McpClient


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
