"""MCP stdio 代理：把 Claude Code 的 stdio MCP 流量动态转发到远端 SGME MCP。

不维护静态工具清单——SGME 新增工具无需改适配器。stdio 固定 UTF-8，
避免 Windows 默认 GBK 编码工具描述时崩溃。
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from . import __version__
from .config import Settings
from .mcp_client import McpClient


def _configure_utf8(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def _response(request_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _handle(request: dict[str, Any], remote: Any) -> str | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        remote.initialize()
        return _response(
            request_id,
            result={
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "claude-sgme", "version": __version__},
                "instructions": (
                    "SGME tools are forwarded through the Claude Code SGME adapter "
                    "(agent_id=claude-code)."
                ),
            },
        )
    if method == "notifications/cancelled":
        return None
    if method == "ping":
        return _response(request_id, result={})
    if method == "tools/list":
        return _response(request_id, result=remote.list_tools())
    if method == "tools/call":
        params = request.get("params") or {}
        return _response(
            request_id,
            result=remote.call_tool(params["name"], params.get("arguments") or {}),
        )
    return _response(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def serve(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    remote: Any | None = None,
) -> None:
    _configure_utf8(stdin)
    _configure_utf8(stdout)
    settings = Settings.from_env()
    # 缺 key 时也先启动，让具体工具调用返回清晰错误（而不是服务器直接死掉）
    client = remote or McpClient(settings.mcp_url, settings.api_key)
    try:
        for line in stdin:
            if not line.strip():
                continue
            request = json.loads(line)
            try:
                rendered = _handle(request, client)
            # MCP 边界必须把任意后端异常转成 JSON-RPC error
            except Exception as exc:  # noqa: BLE001
                rendered = _response(
                    request.get("id"),
                    error={"code": -32000, "message": str(exc)[:500]},
                )
            if rendered is not None:
                stdout.write(rendered + "\n")
                stdout.flush()
    finally:
        if remote is None:
            client.close()


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
