"""tests/test_security.py：入口安全加固测试基座（ST-55，方案 v3）。

本文件为 ST-55 安全加固批次（T-255 ~ T-266）的共享测试面，约定（S5，方案 §四）：

1. SecurityState 实例随 create_app 创建（T-262 起挂 app.state.security_state）、
   可注入可重置；HTTP/MCP 两 app 共享同一实例——测试不得各自 new 单例互踩。
2. TestClient 的 client.host 为 "testclient"，与 net_guard.LOCALHOST_HOSTS 归同一
   「本地」组（is_loopback_source 返回 True）——集成用例默认视为回环来源，不会因
   失败累计触发封禁。
3. 需要跨阈值行为的用例（封禁/升级阶梯）必须【显式设置配置】
   （如 auth_fail_threshold=20）；缺省 threshold=0 = 观察期（只计数不封）。
4. 显式阈值用例结束前后注意重置共享状态（实例注入重建或调用 reset 接口），
   防同进程内用例间串扰。

涉及卡片：
- T-255：请求体上限（I3 前置 3 项）
- T-256：resolve_client_ip / ip_key（U1，追加测试类）
- T-258：security_state 状态机（U2 单元部分，追加）
- T-260：SecurityGate（U2 / U3 / U9，追加）
- T-261：常数时间比较（U8，追加）
- T-262 / T-263：接线集成（I1 / I2 / I3 收口，追加）
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.net_guard import (
    ip_key,
    is_loopback_source,
    parse_trusted_proxies,
    resolve_client_ip,
)
from sgme.server.app import create_app

# ---------- 夹具（S5 基座约定） ----------


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path，不触真实 data/）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


# ---------- I3：请求体上限（T-255，§2.8-1 / 细节 9-10 校准） ----------


class TestRequestBodyLimit:
    """请求体上限：框架原生 RequestBodyLimitMiddleware。

    断言口径（校准结论）：
    - 413 形态 = text/plain "Content Too Large"（框架默认，非 JSON detail）；
    - chunked（无 content-length）超限同样 413，挂点 = 读取请求体的业务端点
      （/v1/append）；不读 body 的路径不触发 413 属预期。
    """

    def _make_client(self, conns, cfg, tmp_path, max_body_bytes: int) -> TestClient:
        mem_conn, session_conn, wiki_conn = conns
        cfg["server"]["max_body_bytes"] = max_body_bytes
        app = create_app(
            cfg=cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
            wiki_conn=wiki_conn,
            admin_key="test-admin-key-security",
            agent_key="test-agent-key-security",
            bearer_token="",
            agent_store_path=tmp_path / "agent_keys.json",
        )
        return TestClient(app)

    def test_oversize_returns_413(self, conns, cfg, tmp_path):
        """非 chunked（带 content-length）超限 → 413。"""
        client = self._make_client(conns, cfg, tmp_path, max_body_bytes=1024)
        r = client.post(
            "/v1/append",
            json={
                "session_key": "sec-t255-a",
                "started_at": "2026-10-11T00:00:00Z",
                "content": "# 2026-10-11T00:00:00Z user\n" + "x" * 4096,
            },
            headers={"X-API-Key": "test-agent-key-security"},
        )
        assert r.status_code == 413
        assert "text/plain" in r.headers.get("content-type", "")
        assert "Content Too Large" in r.text

    def test_chunked_oversize_returns_413(self, conns, cfg, tmp_path):
        """chunked（无 content-length）超限 → 413（挂 /v1/append 读 body 端点）。"""
        client = self._make_client(conns, cfg, tmp_path, max_body_bytes=1024)
        payload = (
            '{"session_key":"sec-t255-b","started_at":"2026-10-11T00:00:00Z","content":"'
            + "x" * 4096
            + '"}'
        )
        body = payload.encode()
        r = client.post(
            "/v1/append",
            content=body,
            headers={
                "X-API-Key": "test-agent-key-security",
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        assert r.status_code == 413
        assert "text/plain" in r.headers.get("content-type", "")
        assert "Content Too Large" in r.text

    def test_small_request_passes(self, conns, cfg, tmp_path):
        """上限内正常请求 → 200（/v1/health 不需要 key，直接可用）。"""
        client = self._make_client(conns, cfg, tmp_path, max_body_bytes=1024)
        r = client.get("/v1/health")
        assert r.status_code == 200

    def test_zero_disables_limit(self, conns, cfg, tmp_path):
        """max_body_bytes=0 → 不装配体限中间件（显式关闭语义）。"""
        client = self._make_client(conns, cfg, tmp_path, max_body_bytes=0)
        body = b"x" * 4096
        r = client.post(
            "/v1/append",
            content=body,
            headers={
                "X-API-Key": "test-agent-key-security",
                "Content-Type": "application/json",
            },
        )
        # 体限关闭：不因大小 413（业务校验失败形态为 4xx JSON，非 text/plain 413）
        assert not (
            r.status_code == 413 and "text/plain" in r.headers.get("content-type", "")
        )


# ---------- U1：来源解析共享叶子（T-256，§2.1 K3/K6/D5） ----------


class TestResolveClientIp:
    """resolve_client_ip 定稿算法全项（K6）+ 归一化/聚合（D5）+ 本地判定（S5 归类）。"""

    # 名单：一个 /24 私网段 + 单 IP（覆盖 CIDR 与单 IP 两种形态）
    PROXIES = parse_trusted_proxies(["10.0.0.0/24", "10.9.9.9"])

    def test_no_proxies_uses_peer(self):
        """无名单 → 直接采用连接对端，不解析任何转发头。"""
        assert resolve_client_ip("203.0.113.5", "1.2.3.4, 5.6.7.8", []) == "203.0.113.5"

    def test_peer_not_in_proxies_uses_peer(self):
        """对端不在名单 → 采用连接对端（不信任其 XFF）。"""
        assert (
            resolve_client_ip("203.0.113.5", "1.2.3.4", self.PROXIES) == "203.0.113.5"
        )

    def test_trusted_peer_xff_right_to_left(self):
        """对端可信 → XFF 从右向左，第一个不在名单内的合法 IP 被采用。"""
        # 右起第一个 10.0.0.3 在名单 → 跳过；10.0.1.7 不在 → 采用
        assert (
            resolve_client_ip("10.0.0.2", "10.0.1.7, 10.0.0.3", self.PROXIES)
            == "10.0.1.7"
        )

    def test_cidr_membership(self):
        """CIDR 命中：10.0.0.77 属 10.0.0.0/24 → 跳过，取更左的非名单 IP。"""
        assert (
            resolve_client_ip("10.0.0.2", "198.51.100.1, 10.0.0.77", self.PROXIES)
            == "198.51.100.1"
        )

    def test_single_ip_proxy(self):
        """单 IP 名单（10.9.9.9）精确命中。"""
        assert (
            resolve_client_ip("10.9.9.9", "198.51.100.2", self.PROXIES)
            == "198.51.100.2"
        )

    def test_all_tokens_trusted_falls_back_to_peer(self):
        """全部 token 都在名单内 → 回退连接对端。"""
        assert (
            resolve_client_ip("10.0.0.2", "10.0.0.3, 10.0.0.4", self.PROXIES)
            == "10.0.0.2"
        )

    def test_illegal_token_falls_back_to_peer(self):
        """非法 token → 伪造串，立即回退连接对端（不得跳过继续向左）。"""
        # 右起第一个 token 非法：即使更左侧有合法非名单 IP 也不采用
        assert (
            resolve_client_ip("10.0.0.2", "198.51.100.3, not-an-ip", self.PROXIES)
            == "10.0.0.2"
        )

    def test_forged_chain_falls_back_to_peer(self):
        """伪造完整 XFF 链（全非法）→ 回退连接对端。"""
        assert (
            resolve_client_ip("10.0.0.2", "evil, [::1]:8080, 1.2.3.4:99", self.PROXIES)
            == "10.0.0.2"
        )

    def test_no_xff_falls_back_to_peer(self):
        """无 XFF / 空 XFF → 回退连接对端。"""
        assert resolve_client_ip("10.0.0.2", None, self.PROXIES) == "10.0.0.2"
        assert resolve_client_ip("10.0.0.2", "  ", self.PROXIES) == "10.0.0.2"

    def test_ipv4_mapped_normalization(self):
        """::ffff:a.b.c.d 归一化为纯 IPv4（D5）。"""
        assert (
            resolve_client_ip(
                "10.0.0.2",
                "::ffff:198.51.100.4",
                parse_trusted_proxies(["10.0.0.0/24"]),
            )
            == "198.51.100.4"
        )

    def test_non_ip_peer_returned_as_is(self):
        """非 IP 对端串（testclient）原样返回、不做 ipaddress 转换（S5 归类）。"""
        assert resolve_client_ip("testclient", "1.2.3.4", self.PROXIES) == "testclient"


class TestIpKeyAggregation:
    """ip_key /64 聚合（D5/§十·二）：同 /64 同键、跨 /64 异键、IPv4 原样。"""

    def test_ipv4_unchanged(self):
        assert ip_key("198.51.100.4") == "198.51.100.4"

    def test_non_ip_passthrough(self):
        assert ip_key("testclient") == "testclient"

    def test_ipv6_same_slash64_same_key(self):
        a = ip_key("2001:db8:abcd:0012::1")
        b = ip_key("2001:db8:abcd:0012::ffff")
        assert a == b
        assert a.endswith("/64")

    def test_ipv6_diff_slash64_diff_key(self):
        a = ip_key("2001:db8:abcd:0012::1")
        b = ip_key("2001:db8:abcd:0013::1")
        assert a != b

    def test_ipv4_mapped_key_is_ipv4(self):
        assert ip_key("::ffff:198.51.100.4") == "198.51.100.4"


class TestLoopbackSource:
    """is_loopback_source：解析后来源「本地」判定（与 Host 头判定分工）。"""

    def test_ipv4_loopback(self):
        assert is_loopback_source("127.0.0.1")
        assert is_loopback_source("127.8.8.8")

    def test_ipv6_loopback(self):
        assert is_loopback_source("::1")

    def test_testclient(self):
        assert is_loopback_source("testclient")

    def test_remote_not_local(self):
        assert not is_loopback_source("203.0.113.5")
        assert not is_loopback_source("10.0.0.2")

    def test_none_empty(self):
        assert not is_loopback_source(None)
        assert not is_loopback_source("")

    def test_mapped_v4_loopback(self):
        assert is_loopback_source("::ffff:127.0.0.1")


class TestParseTrustedProxies:
    """parse_trusted_proxies 纯函数（校验告警与缓存归 T-257）。"""

    def test_cidr_and_single_ip(self):
        nets = parse_trusted_proxies(["10.0.0.0/24", "10.9.9.9"])
        assert len(nets) == 2

    def test_illegal_items_skipped(self):
        nets = parse_trusted_proxies(["not-a-net", "", "300.1.1.1", "10.0.0.0/24"])
        assert len(nets) == 1

    def test_none_empty(self):
        assert parse_trusted_proxies(None) == []
        assert parse_trusted_proxies([]) == []

    def test_host_bits_tolerated(self):
        """strict=False：10.0.0.77/24 主机位非零写法合法。"""
        nets = parse_trusted_proxies(["10.0.0.77/24"])
        assert len(nets) == 1


# ---------- I3 收口位（T-262 落） ----------
# 「413 不产生 401/403 失败计数副作用」为跨卡断言：
# 本卡落 413 形态部分；完整副作用断言在 T-262（接线后）追加到此类。
