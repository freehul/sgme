import io
import json

from codex_sgme.mcp_proxy import serve


class FakeRemote:
    def __init__(self):
        self.calls = []

    def initialize(self):
        self.calls.append(("initialize", {}))
        return {"serverInfo": {"name": "SGME", "version": "1.4.3"}}

    def list_tools(self):
        self.calls.append(("tools/list", {}))
        return {"tools": [{"name": "health", "inputSchema": {"type": "object"}}]}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}


class WarningRemote(FakeRemote):
    def list_tools(self):
        self.calls.append(("tools/list", {}))
        return {
            "tools": [
                {
                    "name": "skill_materialize",
                    "description": "\u26a0 server-side filesystem warning",
                    "inputSchema": {"type": "object"},
                }
            ]
        }


def test_stdio_proxy_forwards_mcp_initialize_list_and_call():
    incoming = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05"},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "health", "arguments": {}},
                    }
                ),
            ]
        )
        + "\n"
    )
    outgoing = io.StringIO()
    remote = FakeRemote()

    serve(incoming, outgoing, remote)

    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[1]["result"]["tools"][0]["name"] == "health"
    assert responses[2]["result"]["content"][0]["text"] == "ok"
    assert remote.calls == [
        ("initialize", {}),
        ("tools/list", {}),
        ("health", {}),
    ]


def test_stdio_proxy_reconfigures_gbk_streams_to_utf8():
    incoming = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    raw = io.BytesIO()
    outgoing = io.TextIOWrapper(raw, encoding="gbk")

    serve(incoming, outgoing, WarningRemote())
    outgoing.flush()

    assert "\u26a0".encode("utf-8") in raw.getvalue()


def test_stdio_proxy_returns_method_not_found_error():
    incoming = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "unknown"}) + "\n"
    )
    outgoing = io.StringIO()

    serve(incoming, outgoing, FakeRemote())

    response = json.loads(outgoing.getvalue())
    assert response["id"] == 7
    assert response["error"]["code"] == -32601
