"""SGME 网络地址统一解析（避免各脚本硬编码主机地址）。

优先级：环境变量 → ~/.sgme/install.json → 开发默认（localhost:9910）。
以后 SGME 迁移/换机，只需改环境变量或 install.json，无需改任何代码。

环境变量：
  SGME_HTTP_URL   完整 HTTP base，如 http://192.168.10.10:9910（最高优先）
  SGME_MCP_URL    完整 MCP endpoint，如 http://192.168.10.10:9913/mcp
  SGME_HTTP_HOST / SGME_HTTP_PORT / SGME_MCP_HOST / SGME_MCP_PORT
  SGME_INSTALL_JSON   覆盖 install.json 路径（默认 ~/.sgme/install.json）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_HOST = "localhost"
DEFAULT_HTTP_PORT = 9910
DEFAULT_MCP_PORT = 9913


def _read_install_json() -> dict:
    p = Path(os.environ.get("SGME_INSTALL_JSON", "~/.sgme/install.json")).expanduser()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def sgme_http_base() -> str:
    """HTTP API base，如 http://192.168.10.10:9910（无尾部斜杠）。"""
    url = os.environ.get("SGME_HTTP_URL")
    if url:
        return url.rstrip("/")
    cfg = _read_install_json().get("http", {})
    host = os.environ.get("SGME_HTTP_HOST") or cfg.get("host") or DEFAULT_HOST
    port = int(os.environ.get("SGME_HTTP_PORT") or cfg.get("port") or DEFAULT_HTTP_PORT)
    return f"http://{host}:{port}"


def sgme_mcp_url() -> str:
    """MCP endpoint，如 http://192.168.10.10:9913/mcp。"""
    url = os.environ.get("SGME_MCP_URL")
    if url:
        return url.rstrip("/")
    cfg = _read_install_json().get("mcp", {})
    host = os.environ.get("SGME_MCP_HOST") or cfg.get("host") or DEFAULT_HOST
    port = int(os.environ.get("SGME_MCP_PORT") or cfg.get("port") or DEFAULT_MCP_PORT)
    return f"http://{host}:{port}/mcp"
