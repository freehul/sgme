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

from typing import ClassVar

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


# ---------- U2 状态机单元 / U7 状态有界（T-258，§2.2 K1/K2/K5） ----------


class TestSecurityState:
    """SecurityState 状态机（U2 状态机单元部分 + U7 状态有界部分）。

    时间全部用假 now 参数驱动（不 sleep）；阈值每次显式传入（构造与阈值分离）。
    参数口径：window=300 / ban=100 / ban_max=3600 / reset=86400。
    """

    P: ClassVar[dict] = {
        "window_sec": 300,
        "ban_seconds": 100,
        "ban_max_seconds": 3600,
        "ban_reset_sec": 86400,
    }

    @staticmethod
    def _flood(state, key, start, threshold=5):
        """从 start 时刻起连打 threshold 次失败触发封禁，返回封禁决策。"""
        decision = {}
        for i in range(threshold):
            decision = state.record_failure(
                key, now=start + i, threshold=threshold, **TestSecurityState.P
            )
        return decision

    def test_observation_period_counts_only(self):
        """threshold=0 = 观察期（K2）：只计数不封。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        for i in range(50):
            d = s.record_failure("203.0.113.1", now=i, threshold=0, **self.P)
            assert d["banned"] is False
            assert d["fail_count"] == i + 1
        assert s.is_banned("203.0.113.1", now=50) == (False, 0, 0)

    def test_ban_triggered_at_threshold(self):
        """达阈值触发封禁：基础时长 / level=1 / 未升级。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        d = self._flood(s, "203.0.113.2", start=0)
        assert d["banned"] is True
        assert d["duration"] == 100.0
        assert d["escalated"] is False
        assert d["level"] == 1
        assert s.is_banned("203.0.113.2", now=4) == (True, 100, 1)

    def test_ban_expires_then_escalates_within_reset_window(self):
        """封禁 → 到期解除 → reset 窗口内再触发 → 时长加倍（K1）。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        d1 = self._flood(s, "203.0.113.3", start=0)
        assert d1["duration"] == 100.0 and d1["level"] == 1
        # 到期解除（禁期至 104：0~4 连打，4 时刻触发封 100s）
        assert s.is_banned("203.0.113.3", now=103.5) == (True, 1, 1)
        assert s.is_banned("203.0.113.3", now=104) == (False, 0, 0)
        # reset 窗口内（100 → 204 ≤ 86400）再触发 → ×2
        d2 = self._flood(s, "203.0.113.3", start=200)
        assert d2["banned"] is True
        assert d2["duration"] == 200.0
        assert d2["escalated"] is True
        assert d2["level"] == 2

    def test_escalation_caps_at_max(self):
        """升级阶梯 ×2 封顶 ban_max_seconds=3600；封顶后仍记 escalated。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        now = 0.0
        durations = []
        for _ in range(10):
            d = self._flood(s, "203.0.113.4", start=now)
            assert d["banned"] is True
            durations.append(d["duration"])
            now = d["until"] + 10.0  # 每轮到期后小间隔再触发（均在 reset 窗口内）
        # 100→200→400→800→1600→3200→3600（封顶后维持）
        assert durations == [
            100.0,
            200.0,
            400.0,
            800.0,
            1600.0,
            3200.0,
            3600.0,
            3600.0,
            3600.0,
            3600.0,
        ]
        assert all(d <= 3600.0 for d in durations)

    def test_ladder_resets_after_clean_period(self):
        """清洁期满（连续 reset 窗口无新封禁）→ 阶梯归零，回基础时长 / level=1。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        d1 = self._flood(s, "203.0.113.5", start=0)
        assert d1["duration"] == 100.0
        d2 = self._flood(s, "203.0.113.5", start=200)  # 升级到 200
        assert d2["duration"] == 200.0 and d2["level"] == 2
        # 等 reset 窗口（86400）过完再触发 → 归零
        clean_start = d2["until"] + 86400 + 10
        d3 = self._flood(s, "203.0.113.5", start=clean_start)
        assert d3["duration"] == 100.0
        assert d3["escalated"] is False
        assert d3["level"] == 1

    def test_window_expiry_does_not_trigger(self):
        """窗口过期：散点失败不累计触发（window_sec=300 内过期戳惰性清除）。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        for i in range(4):
            s.record_failure("203.0.113.6", now=i * 400, threshold=5, **self.P)
        d = s.record_failure("203.0.113.6", now=1600, threshold=5, **self.P)
        assert d["banned"] is False
        assert d["fail_count"] == 1  # 旧戳全部出窗，仅剩本次

    def test_in_ban_failure_not_counted(self):
        """禁期内 record_failure 不覆盖既有封禁（防御兜底；调用侧 T-260 先查再计数）。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        d1 = self._flood(s, "203.0.113.7", start=0)
        assert d1["banned"] is True
        for i in range(10):
            d = s.record_failure("203.0.113.7", now=10 + i, threshold=5, **self.P)
            assert d["banned"] is False
        # 封禁决策不变（until 仍是原值 104），is_banned 一致
        assert s.is_banned("203.0.113.7", now=15) == (True, 89, 1)

    def test_u7_flood_bounded_with_warning(self, caplog):
        """U7（K5）：随机 key 洪泛后状态尺寸有界（≤ 上限），丢最旧 + WARNING。"""
        import logging as _logging
        import random

        from sgme.security_state import SecurityState

        s = SecurityState(max_entries=500)
        rng = random.Random(42)
        with caplog.at_level(_logging.WARNING, logger="sgme.security_state"):
            for i in range(2000):
                key = f"198.51.100.{rng.randint(0, 9999)}"
                s.record_failure(
                    key, now=i * 0.1, threshold=0, **self.P
                )  # 观察期：只计数
        snap = s.snapshot(now=2000)
        assert snap["fail_keys"] <= 500
        assert snap["size"] <= 500
        assert snap["max_entries"] == 500
        warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
        assert warnings, "超限丢最旧必须打 WARNING"

    def test_u7_ban_entries_removed_after_reset_window(self):
        """U7（K5）：封禁条目在「禁期 + reset 窗口」后删除（低频全扫路径）。"""
        from sgme.security_state import FULL_SWEEP_INTERVAL, SecurityState

        s = SecurityState()
        d = self._flood(s, "203.0.113.8", start=0)
        until = d["until"]
        # 推到「禁期 + reset 窗口」之后，且凑一次全扫触发
        target = until + 86400 + 1
        writes = 0
        while (
            s.is_banned("203.0.113.8", now=target)[0] is False
            and writes < FULL_SWEEP_INTERVAL + 1
        ):
            s.record_failure(
                "203.0.113.9", now=target + writes, threshold=10**9, **self.P
            )
            writes += 1
        snap = s.snapshot(now=target)
        assert snap["ban_entries"] == 0, "禁期+reset 窗口过后封禁条目必须被清扫"

    def test_snapshot_shape(self):
        """快照形状：活封禁列表（key/剩余秒/层级）+ 计数概览（T-265 status 数据源）。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        d = self._flood(s, "203.0.113.10", start=0)
        s.record_failure(
            "203.0.113.11", now=1, threshold=0, **self.P
        )  # 观察期计数 1 条
        snap = s.snapshot(now=40)
        assert set(snap) == {
            "active_bans",
            "fail_counts",
            "fail_keys",
            "ban_entries",
            "size",
            "max_entries",
        }
        assert snap["active_bans"] == [
            {"key": "203.0.113.10", "remaining_sec": 64, "level": 1}
        ]
        assert snap["fail_counts"] == {"203.0.113.11": 1}
        assert snap["fail_keys"] == 1
        assert snap["ban_entries"] == 1
        assert snap["size"] == 2
        # 过期后活封禁清空（条目仍在，等 reset 窗口过完才删）
        later = s.snapshot(now=d["until"] + 1)
        assert later["active_bans"] == []
        assert later["ban_entries"] == 1

    def test_time_injection_no_wallclock(self):
        """时间外部注入：旧 now 也能回放（可复现），不依赖墙钟。"""
        from sgme.security_state import SecurityState

        s = SecurityState()
        self._flood(s, "203.0.113.12", start=1000)
        # 同一场景重放到新实例，结果逐字段一致
        s2 = SecurityState()
        self._flood(s2, "203.0.113.12", start=1000)
        snap1, snap2 = s.snapshot(now=1004), s2.snapshot(now=1004)
        assert snap1 == snap2
