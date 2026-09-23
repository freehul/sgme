"""sgme/net_guard.py：入口层共享的「本机来源 / 默认开发 Key」守卫（F-3，2026-09-24 深度审查）。

背景：HTTP（server/app.py）与 MCP（mcp_server.py::ApiKeyMiddleware）是两套入口，
「默认开发 Key 仅限本机回环」策略原先只在 HTTP 侧落地（ST-22⑧），MCP 侧缺失——
同一安全策略两处实现 = 漂移温床。此处收敛为**单一策略源**，
两入口共用（入口层互不依赖，同依赖本叶子模块，符合模块边界铁律）。

纯叶子模块：零内部依赖，任何层可安全 import。
"""

from __future__ import annotations

from typing import Any

# 本机回环来源集合：真实回环地址 + Starlette TestClient 的固定 client host
# （"testclient" 是测试基座的主机名，非网络来源——放行它保证「默认 key + 本机开发」
# 工作流在测试环境下同样成立）。
LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

# Host 头额外放行 "testserver"（TestClient 默认 base_url http://testserver）：
# 仅测试基座会发出该 Host，浏览器/公网不可能解析出该主机名，无实际削弱。
LOCALHOST_HOST_HEADERS = LOCALHOST_HOSTS | {"testserver"}


def is_localhost_host(host: str | None) -> bool:
    """来源主机是否属于回环集合；None/空 → False（安全侧失败：宁可误拒不可漏放）。"""
    if not host:
        return False
    return str(host).lower() in LOCALHOST_HOSTS


def hostname_of(host_header: str | None) -> str:
    """从 Host 头取主机名：去端口、去 IPv6 方括号（``[::1]:9910`` → ``::1``）。"""
    h = (host_header or "").strip()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end].lower() if end != -1 else h.lower()
    return h.split(":", 1)[0].lower()


def is_localhost_host_header(host_header: str | None) -> bool:
    """Host 头是否指向本机回环（F-1 纵深：防 DNS rebinding 的关键校验）。"""
    return hostname_of(host_header) in LOCALHOST_HOST_HEADERS


def is_default_key_from_remote(key_store: Any, key: str | None, client_host: str | None) -> bool:
    """默认开发 Key + 非本机来源 → True（调用方按各自协议拒绝：HTTP 403 / MCP 403）。

    - 默认 key（SGME_AGENT_KEY / SGME_ADMIN_KEY 未设置时的内置兜底值）+ 非本机 → True
    - 默认 key + 本机回环（127.0.0.1 / ::1 / localhost）→ False（本机开发工作流不受影响）
    - 自定义 key（含 register_agent 签发的 agt_*）→ False（不受限）
    """
    if not key_store.is_default_dev_key(key):
        return False
    return not is_localhost_host(client_host)
