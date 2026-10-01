from __future__ import annotations

import json
from typing import Any

import httpx

from . import __version__


class McpError(RuntimeError):
    pass


class McpClient:
    """SGME streamable-HTTP MCP 的最小客户端（官方 SDK 语义，httpx 直连）。"""

    def __init__(
        self,
        url: str,
        api_key: str | None,
        *,
        timeout: float = 10.0,
        session: Any | None = None,
    ):
        self.url = url
        self.api_key = api_key
        self.session = session or httpx.Client(timeout=timeout, trust_env=False)
        self.session_id: str | None = None
        self._next_id = 1

    def _post(
        self, body: dict[str, Any], *, _retry_stale_session: bool = True
    ) -> dict[str, Any]:
        if not self.api_key:
            raise ValueError("SGME MCP 调用需要 agent key（SGME_CLAUDE_KEY）")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = self.session.post(self.url, headers=headers, json=body)
        if (
            response.status_code == 404
            and self.session_id
            and _retry_stale_session
            and "Session not found" in (getattr(response, "text", "") or "")
        ):
            # 服务端重启/会话过期：带旧会话的请求未被执行，安全丢弃后重初始化重试一次
            self.session_id = None
            self.initialize()
            return self._post(body, _retry_stale_session=False)
        if response.status_code >= 400:
            detail = getattr(response, "text", "request failed") or "request failed"
            raise McpError(f"SGME MCP HTTP {response.status_code}: {detail[:500]}")
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id
        return self._parse_event_stream(response.text)

    @staticmethod
    def _parse_event_stream(text: str) -> dict[str, Any]:
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
        raise McpError("SGME MCP response did not contain a data event")

    def initialize(self) -> dict[str, Any]:
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "claude-sgme-adapter",
                        "version": __version__,
                    },
                },
            }
        )
        self._next_id += 1
        if "error" in result:
            raise McpError(result["error"].get("message", "initialize failed"))
        return result["result"]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if not self.session_id:
            self.initialize()
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        self._next_id += 1
        if "error" in result:
            raise McpError(result["error"].get("message", "tool call failed"))
        tool_result = result["result"]
        if tool_result.get("isError"):
            raise McpError("SGME MCP tool returned an error")
        return tool_result

    def list_tools(self) -> dict[str, Any]:
        if not self.session_id:
            self.initialize()
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/list",
                "params": {},
            }
        )
        self._next_id += 1
        if "error" in result:
            raise McpError(result["error"].get("message", "tools/list failed"))
        return result["result"]

    def close(self) -> None:
        self.session.close()
