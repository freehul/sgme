# -*- coding: utf-8 -*-
"""MCP stdio 代理：JSON-RPC 帧处理与动态透传语义。"""
import json

from claude_sgme import mcp_proxy


class FakeRemote:
    def initialize(self):
        return {"ok": True}

    def list_tools(self):
        return {"tools": [{"name": "health"}]}

    def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": name}], "isError": False}


def test_initialize_advertises_claude_sgme():
    init = json.loads(
        mcp_proxy._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, FakeRemote())
    )
    assert init["result"]["serverInfo"]["name"] == "claude-sgme"
    assert "claude-code" in init["result"]["instructions"]


def test_notifications_and_ping():
    assert mcp_proxy._handle({"method": "notifications/initialized"}, FakeRemote()) is None
    assert mcp_proxy._handle({"method": "notifications/cancelled"}, FakeRemote()) is None
    ping = json.loads(mcp_proxy._handle({"id": 2, "method": "ping"}, FakeRemote()))
    assert ping["result"] == {}


def test_tools_list_and_call_forward_verbatim():
    listed = json.loads(mcp_proxy._handle({"id": 3, "method": "tools/list"}, FakeRemote()))
    assert listed["result"]["tools"][0]["name"] == "health"
    called = json.loads(
        mcp_proxy._handle(
            {
                "id": 4,
                "method": "tools/call",
                "params": {"name": "agent_onboarding", "arguments": {}},
            },
            FakeRemote(),
        )
    )
    assert called["result"]["content"][0]["text"] == "agent_onboarding"


def test_unknown_method_returns_jsonrpc_error():
    missing = json.loads(mcp_proxy._handle({"id": 5, "method": "unknown/method"}, FakeRemote()))
    assert missing["error"]["code"] == -32601
