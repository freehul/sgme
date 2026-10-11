"""sgme/net_guard.py：入口层共享的「本机来源 / 默认开发 Key」守卫（F-3，2026-09-24 深度审查）。

背景：HTTP（server/app.py）与 MCP（mcp_server.py::ApiKeyMiddleware）是两套入口，
「默认开发 Key 仅限本机回环」策略原先只在 HTTP 侧落地（ST-22⑧），MCP 侧缺失——
同一安全策略两处实现 = 漂移温床。此处收敛为**单一策略源**，
两入口共用（入口层互不依赖，同依赖本叶子模块，符合模块边界铁律）。

纯叶子模块：零内部依赖，任何层可安全 import。
"""

from __future__ import annotations

from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
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


# ---------------------------------------------------------------------------
# 来源解析共享叶子（ST-55 / T-256，方案 §2.1 K3/K6/D5）
#
# 四处消费方（接线于 T-260 / T-262 / T-263，本卡只落叶子与单测）：
#   1. SecurityGate 来源键（T-260）
#   2. net_guard.is_default_key_from_remote 的传参（改喂「解析后来源」）
#   3. app.py::_is_localhost_source（T-262 改用解析后来源判定）
#   4. 封禁回环豁免（T-260，消费点 4）
#
# XFF 定稿算法（K6，§2.1，禁取最左侧）：
#   - trusted_proxies 为空 → 直接采用连接对端（不解析任何转发头）；
#   - 非空且对端不在名单 → 采用连接对端；
#   - 对端在名单 → 解析 X-Forwarded-For，从右向左逐 token：
#     命中名单 → 跳过；第一个不在名单内的合法 IP → 采用；
#     全部 token 在名单内 / 遇非法 token / 无 XFF / 空 XFF → 回退连接对端。
#     伪造串不得「跳过继续向左」——非法 token 即回退（防伪造注入任意来源）。
# ---------------------------------------------------------------------------


def _parse_ip(token: str) -> IPv4Address | IPv6Address | None:
    """token → IP 对象；非法 / 带端口 / 带方括号 → None（按非法处理）。"""
    t = token.strip()
    if not t:
        return None
    try:
        return ip_address(t)
    except ValueError:
        return None


def _normalize_ip(ip: IPv4Address | IPv6Address) -> str:
    """归一化（D5）：IPv4-mapped IPv6（::ffff:a.b.c.d）→ 纯 IPv4；其余压缩形式字符串。"""
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def resolve_client_ip(
    peer_host: str | None,
    xff_header: str | None,
    trusted_proxies: list[IPv4Network | IPv6Network] | list[Any],
) -> str:
    """解析「客户端真实来源」（K3 共享叶子；不 import config——trusted_proxies 由调用方注入已解析列表）。

    返回值语义：
    - 归一化后的 IP 字符串（对端或 XFF 解析结果），或
    - 非 IP 对端串（如 "testclient"）原样返回、不做 ipaddress 转换（沿用 net_guard 归类）。
    """
    peer = (peer_host or "").strip()
    peer_ip = _parse_ip(peer) if peer else None

    # 名单为空：直接采用连接对端（不解析任何转发头）
    if not trusted_proxies:
        return peer

    # 对端不在名单（或非 IP 串）：采用连接对端
    if peer_ip is None or not any(peer_ip in net for net in trusted_proxies):
        return peer

    # 对端可信 → 从右向左解析 XFF
    tokens = [t.strip() for t in (xff_header or "").split(",")]
    if not tokens or not any(tokens):
        return peer
    for token in reversed(tokens):
        ip = _parse_ip(token)
        if ip is None:
            # 非法 token（含带端口 / 方括号形态）→ 伪造串，立即回退连接对端（不跳过继续向左）
            return peer
        if any(ip in net for net in trusted_proxies):
            continue  # 名单内 → 跳过
        return _normalize_ip(ip)
    # 全部 token 都在名单内 → 回退连接对端
    return peer


def ip_key(source: str) -> str:
    """封禁与限流键（D5/§十·二）：IPv6 按 /64 聚合（防前缀内轮换）、IPv4 原样、非 IP 串原样。

    per-key 键与工具键不参与本聚合。
    """
    ip = _parse_ip(source) if source else None
    if ip is None:
        return source
    if isinstance(ip, IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        # strict=False：用地址本身构造 /64 网络，取网络地址（前 64 位）做键
        return str(IPv6Network((int(ip) >> 64) << 64).network_address) + "/64"
    return str(ip)


def is_loopback_source(source: str | None) -> bool:
    """「本地」判定（解析后来源用）：127.0.0.0/8、::1、"testclient"。

    与既有 is_localhost_host（Host 头用，含 "localhost" 名）分工：
    解析后来源是 IP/对端串，不含 "localhost" 主机名形态——判定一律走本函数。
    """
    if not source:
        return False
    s = source.strip().lower()
    if s == "testclient":
        return True
    ip = _parse_ip(s)
    if ip is None:
        return False
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def parse_trusted_proxies(raw: list[str] | None) -> list[IPv4Network | IPv6Network]:
    """trusted_proxies 配置项 → 网络对象列表（CIDR / 单 IP 均可；非法项跳过）。

    校验告警与缓存归 T-257（本卡只提供纯解析函数）。
    """
    out: list[IPv4Network | IPv6Network] = []
    for item in raw or []:
        t = str(item).strip()
        if not t:
            continue
        try:
            # strict=False 允许 10.0.0.1/24 这类主机位不为零的写法
            out.append(ip_network(t, strict=False))
        except ValueError:
            continue
    return out
