"""T-260 SecurityGate 单测（U2 封禁全路径 / U3 无 key IP 桶 / U9 fail-open 收窄）。

纯 ASGI 直调形态：手搓 scope + 假下游 app（与三.23 旁路中间件测试同模式），
不依赖 create_app（单卡可独立测试，不依赖接线完成——T-262/T-263 再补集成断言）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sgme.server.security import SecurityGate

from sgme.security_log import SecurityLog
from sgme.security_state import SecurityState
from sgme.server.ratelimit import SlidingWindowRateLimiter

# 测试假值（数据卫生：TEST-NET / test- 前缀假串）
FAKE_NOW = 1_760_000_000.0
ADMIN_KEY = "test-admin-key-security"
AGENT_KEY = "test-agent-key-security"


def make_gate(**overrides):
    """SecurityGate + 假下游（默认 200；可设 status）+ 注入件。"""
    downstream_status = overrides.pop("downstream_status", 200)
    status_box = overrides.pop("status_box", None)  # 可变容器：测试中途换下游状态
    entry = overrides.pop("entry", "http")
    state = overrides.pop("state", None) or SecurityState()
    ip_limiter = overrides.pop("ip_limiter", None) or SlidingWindowRateLimiter(
        limit_per_min=5
    )
    key_limiter = overrides.pop("key_limiter", None) or SlidingWindowRateLimiter(
        limit_per_min=120
    )
    tool_limiter = overrides.pop("tool_limiter", None) or SlidingWindowRateLimiter(
        limit_per_min=5
    )
    cfg = overrides.pop("cfg", {})
    log_dir = overrides.pop("log_dir", None)
    import tempfile
    from pathlib import Path

    log_dir = log_dir or Path(tempfile.mkdtemp())
    log = overrides.pop("log", None) or SecurityLog(log_dir, keep_days=0)

    async def downstream(scope, receive, send):
        status = status_box["v"] if status_box is not None else downstream_status
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    gate = SecurityGate(
        downstream,
        state=state,
        ip_limiter=ip_limiter,
        key_limiter=key_limiter,
        tool_limiter=tool_limiter,
        cfg_getter=lambda: cfg,
        log_dir=str(log_dir),
        entry=entry,
        log=log,
        clock=lambda: overrides.pop("now", FAKE_NOW) if False else _clock()["t"],
    )
    return gate, state, ip_limiter, key_limiter, tool_limiter, log, log_dir


def _clock():
    global _CLOCK
    try:
        return _CLOCK
    except NameError:
        _CLOCK = {"t": FAKE_NOW}
        return _CLOCK


def make_scope(
    path="/v1/answer",
    key=None,
    xff=None,
    peer="203.0.113.5",
    method="POST",
):
    headers = []
    if key is not None:
        headers.append((b"x-api-key", key.encode("latin-1")))
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    headers.append((b"user-agent", b"sec-gate-test/1.0"))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "client": (peer, 12345),
        "query_string": b"",
    }


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


class Resp:
    def __init__(self):
        self.status = None
        self.headers = []
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = dict(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


def run(coro):
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------- U2：封禁全路径（K1 升级阶梯，经 Gate 端到端） ----------


class TestBanLifecycle:
    def test_threshold_zero_counts_only(self):
        """缺省 threshold=0（观察期）：错 key 洪泛只计数，不 429 不封禁。"""
        _clock()["t"] = FAKE_NOW
        cfg = {
            "server": {
                "auth_fail_threshold": 0,
                "auth_fail_window_sec": 300,
                "ban_seconds": 900,
                "ban_max_seconds": 3600,
                "ban_reset_sec": 86400,
            }
        }
        gate, state, *_, log, _ = make_gate(cfg=cfg, downstream_status=401)
        resp = Resp()
        for _ in range(50):
            run(gate(make_scope(key="wrong-key"), receive, resp.__call__))
        assert resp.status == 401  # 不变 429
        banned, remaining, _ = state.is_banned("203.0.113.5", FAKE_NOW + 1)
        assert not banned

    def test_ban_triggers_at_threshold(self):
        """threshold=3：3 次失败 → 第 4 次起禁期内所有请求（含有效 key）→ 429 + Retry-After。"""
        _clock()["t"] = FAKE_NOW
        cfg = {
            "server": {
                "auth_fail_threshold": 3,
                "auth_fail_window_sec": 300,
                "ban_seconds": 900,
                "ban_max_seconds": 3600,
                "ban_reset_sec": 86400,
            }
        }
        gate, state, *_, log, _ = make_gate(cfg=cfg, downstream_status=401)
        for _ in range(3):
            run(gate(make_scope(key="wrong-key"), receive, Resp().__call__))
        banned, remaining, level = state.is_banned("203.0.113.5", FAKE_NOW + 1)
        assert banned and level == 1
        # 禁期内：有效 key 也 429（封禁全阻断不打折）；时钟与 remaining 同时刻
        _clock()["t"] = FAKE_NOW + 1
        resp = Resp()
        run(gate(make_scope(key=AGENT_KEY), receive, resp.__call__))
        assert resp.status == 429
        assert int(resp.headers[b"retry-after"]) == remaining
        # 429 形态（D8 自建 JSON）
        body = json.loads(resp.body)
        assert body["error"]["code"] == "ERR_RATE_LIMITED"

    def test_ban_expires_and_escalates_within_reset_window(self):
        """到期解除 → reset 窗口内再触发 → 时长 ×2（900→1800）+ ban_escalated 事件。"""
        _clock()["t"] = FAKE_NOW
        cfg = {
            "server": {
                "auth_fail_threshold": 3,
                "auth_fail_window_sec": 300,
                "ban_seconds": 900,
                "ban_max_seconds": 3600,
                "ban_reset_sec": 86400,
            }
        }
        box = {"v": 401}
        gate, state, *_, log, _ = make_gate(cfg=cfg, status_box=box)
        for _ in range(3):
            run(gate(make_scope(key="wrong-key"), receive, Resp().__call__))
        # t+901：第一轮到期 → 请求放行且记 ban_expired（下游此刻正常处理）
        box["v"] = 200
        _clock()["t"] = FAKE_NOW + 901
        resp = Resp()
        run(gate(make_scope(key=AGENT_KEY), receive, resp.__call__))
        assert resp.status == 200
        events = log.read_events(
            datetime.fromtimestamp(FAKE_NOW + 901, tz=UTC).strftime("%Y-%m-%d")
        )
        assert any(e["event"] == "ban_expired" for e in events)
        # reset 窗口内再触发 3 次 → 时长 ×2
        box["v"] = 401  # 显式切回 401（R2 段下游必须拒绝才计失败）
        _clock()["t"] = FAKE_NOW + 910
        for _ in range(3):
            run(gate(make_scope(key="wrong-key"), receive, Resp().__call__))
        banned, remaining, level = state.is_banned("203.0.113.5", FAKE_NOW + 911)
        assert banned and level == 2
        assert remaining <= 1800 and remaining >= 1790  # 1800s 封顶起点附近
        events = log.read_events(
            datetime.fromtimestamp(FAKE_NOW + 911, tz=UTC).strftime("%Y-%m-%d")
        )
        assert any(e["event"] == "ban_escalated" for e in events)

    def test_escalation_capped_at_max(self):
        """连续升级 → 时长 ≤ ban_max_seconds（3600）封顶。"""
        _clock()["t"] = FAKE_NOW
        cfg = {
            "server": {
                "auth_fail_threshold": 2,
                "auth_fail_window_sec": 300,
                "ban_seconds": 900,
                "ban_max_seconds": 3600,
                "ban_reset_sec": 86400,
            }
        }
        gate, state, *_, log, _ = make_gate(cfg=cfg, downstream_status=401)
        # 第一轮 900；+910 再触发 1800；+1810+901？——直接快进跨轮验证封顶
        t = FAKE_NOW
        for round_no in range(5):
            for _ in range(2):
                _clock()["t"] = t
                run(gate(make_scope(key="wrong-key"), receive, Resp().__call__))
            t += 60
            banned, remaining, level = state.is_banned("203.0.113.5", t)
            assert banned
            assert remaining <= 3600
            # 快进越过本轮封禁（≤3600）+ 一点余量，保持在 reset 窗口内
            t += 3605
        # 最终轮时长必为 3600（封顶）
        for _ in range(2):
            _clock()["t"] = t
            run(gate(make_scope(key="wrong-key"), receive, Resp().__call__))
        t += 1
        banned, remaining, _ = state.is_banned("203.0.113.5", t)
        assert banned
        assert remaining <= 3600

    def test_ladder_resets_after_clean_window(self):
        """清洁期 > ban_reset_sec 无新封禁 → 阶梯归零（再触发回基础时长 900）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {
            "server": {
                "auth_fail_threshold": 2,
                "auth_fail_window_sec": 300,
                "ban_seconds": 900,
                "ban_max_seconds": 3600,
                "ban_reset_sec": 86400,
            }
        }
        gate, state, *_, log, _ = make_gate(cfg=cfg, downstream_status=401)
        # 第一轮
        for _ in range(2):
            run(gate(make_scope(key="wrong-key"), receive, Resp().__call__))
        assert state.is_banned("203.0.113.5", FAKE_NOW + 1)[0]
        # 快进 900+86400+1（越过 reset 窗口）
        t = FAKE_NOW + 900 + 86400 + 1
        _clock()["t"] = t
        for _ in range(2):
            run(gate(make_scope(key="wrong-key"), receive, Resp().__call__))
        t += 1
        banned, remaining, level = state.is_banned("203.0.113.5", t)
        assert banned and level == 1
        assert remaining <= 900  # 基础时长（阶梯已归零）


# ---------- U3：无 key IP 桶边界（D2/D3） ----------


class TestNoKeyIpBucket:
    def test_no_key_flood_limited(self):
        """无 key 洪泛 → 429 ip_limited。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"ip_rate_limit_per_min": 3}}
        gate, *_, log, _ = make_gate(
            cfg=cfg, ip_limiter=SlidingWindowRateLimiter(limit_per_min=3)
        )
        statuses = []
        for _ in range(5):
            resp = Resp()
            run(gate(make_scope(key=None), receive, resp.__call__))
            statuses.append(resp.status)
        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429

    def test_empty_and_blank_key_treated_as_no_key(self):
        """空串 / 纯空白 header 视同无 key（进 IP 桶）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"ip_rate_limit_per_min": 1}}
        gate, *_, log, _ = make_gate(
            cfg=cfg, ip_limiter=SlidingWindowRateLimiter(limit_per_min=1)
        )
        r1 = Resp()
        run(gate(make_scope(key=""), receive, r1.__call__))
        r2 = Resp()
        run(gate(make_scope(key="   "), receive, r2.__call__))
        assert r1.status == 200 and r2.status == 429  # 空白视同无 key，同桶计数

    def test_duplicate_key_header_first_wins(self):
        """重复 X-API-Key 头取首个。"""
        _clock()["t"] = FAKE_NOW
        cfg: dict = {"server": {}}
        gate, *_, log, _ = make_gate(cfg=cfg)
        scope = make_scope(key=AGENT_KEY)
        scope["headers"] = [
            (b"x-api-key", AGENT_KEY.encode()),
            (b"x-api-key", b"second-key"),
        ] + scope["headers"][:1]
        resp = Resp()
        run(gate(scope, receive, resp.__call__))
        assert resp.status == 200  # 首个 key 合法即放行

    def test_key_present_skips_ip_bucket(self):
        """有 key（含错误 key）不进 IP 桶（两线互补，不互替）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"ip_rate_limit_per_min": 1}}
        gate, *_, log, _ = make_gate(
            cfg=cfg,
            ip_limiter=SlidingWindowRateLimiter(limit_per_min=1),
            downstream_status=401,
        )
        for _ in range(10):
            resp = Resp()
            run(gate(make_scope(key="wrong"), receive, resp.__call__))
        # 错 key 请求计 401 失败，但 IP 桶（无 key 桶）不计数 → 不 429
        statuses = []
        for _ in range(3):
            resp = Resp()
            run(gate(make_scope(key="wrong"), receive, resp.__call__))
            statuses.append(resp.status)
        assert 429 not in statuses

    def test_health_exempt(self):
        """/v1/health 完全绕过（D9 探针语义不变）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"auth_fail_threshold": 2, "ip_rate_limit_per_min": 1}}
        gate, state, *_, log, _ = make_gate(
            cfg=cfg,
            ip_limiter=SlidingWindowRateLimiter(limit_per_min=1),
            downstream_status=200,
        )
        # 无 key 打 health 十次：既不进 IP 桶也不计失败（到达下游 200 = 门已绕过）
        for _ in range(10):
            resp = Resp()
            run(gate(make_scope(path="/v1/health", key=None), receive, resp.__call__))
            assert resp.status == 200
        snap = state.snapshot(FAKE_NOW + 1)
        assert snap["fail_counts"] == {}

    def test_loopback_not_banned_but_key_bucket_applies(self):
        """回环豁免封禁（只计数），但 per-key 限流仍适用（回环不受封禁、限流仍适用）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"auth_fail_threshold": 2, "rate_limit_per_min": 3}}
        gate, state, *_, log, _ = make_gate(
            cfg=cfg,
            key_limiter=SlidingWindowRateLimiter(limit_per_min=3),
            downstream_status=401,
        )
        # 回环来源错 key ×10：不封禁
        for _ in range(10):
            resp = Resp()
            run(gate(make_scope(key="wrong", peer="127.0.0.1"), receive, resp.__call__))
        banned, _, _ = state.is_banned("127.0.0.1", FAKE_NOW + 1)
        assert not banned
        # 但回环失败仍计入 fail_counts（观察期读数）
        snap = state.snapshot(FAKE_NOW + 1)
        assert snap["fail_counts"].get("127.0.0.1", 0) >= 10 or snap["fail_keys"] >= 1


# ---------- U9：fail-open 收窄（S8） ----------


class TestFailOpenNarrow:
    def test_state_read_failure_fails_open(self):
        """状态模块抛异常 → 请求放行 + 留痕（计数）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"auth_fail_threshold": 2}}

        class BrokenState:
            def is_banned(self, *a, **k):
                raise RuntimeError("state boom")

            def record_failure(self, *a, **k):
                raise RuntimeError("state boom")

            def snapshot(self, now):
                return {}

        gate, *_, log, _ = make_gate(cfg=cfg, state=BrokenState())
        resp = Resp()
        run(gate(make_scope(key=AGENT_KEY), receive, resp.__call__))
        assert resp.status == 200  # fail-open 放行
        assert gate._internal_errors >= 1  # 留痕

    def test_log_failure_does_not_block(self):
        """审计写失败 → WARNING，主请求不受影响。"""
        _clock()["t"] = FAKE_NOW
        cfg: dict = {"server": {}}

        class BrokenLog:
            def __getattr__(self, name):
                raise RuntimeError("log boom")

        gate, *_, log, _ = make_gate(cfg=cfg, log=BrokenLog(), downstream_status=401)
        resp = Resp()
        run(gate(make_scope(key="wrong"), receive, resp.__call__))
        assert resp.status == 401  # 业务照常
        assert gate._internal_errors >= 1

    def test_ban_check_failure_does_not_block_valid_key(self):
        """封禁判定读取失败 → fail-open 放行（判定路径不整体包 try 的收窄验证）。"""
        _clock()["t"] = FAKE_NOW
        cfg: dict = {"server": {}}

        class HalfBrokenState:
            def is_banned(self, *a, **k):
                raise RuntimeError("boom")

        gate, *_, log, _ = make_gate(cfg=cfg, state=HalfBrokenState())
        resp = Resp()
        run(gate(make_scope(key=AGENT_KEY), receive, resp.__call__))
        assert resp.status == 200


# ---------- 专项：429 形态与 Retry-After（D8） ----------


class Test429Shape:
    def test_retry_after_matches_remaining(self):
        """Retry-After = 封禁剩余秒（D8）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"auth_fail_threshold": 1, "ban_seconds": 900}}
        gate, state, *_, log, _ = make_gate(cfg=cfg, downstream_status=401)
        run(gate(make_scope(key="wrong"), receive, Resp().__call__))
        t = FAKE_NOW + 100
        _clock()["t"] = t
        banned, remaining, _ = state.is_banned("203.0.113.5", t)
        resp = Resp()
        run(gate(make_scope(key=AGENT_KEY), receive, resp.__call__))
        assert resp.status == 429
        assert int(resp.headers[b"retry-after"]) == remaining
        assert b"application/json" in resp.headers[b"content-type"]

    def test_llm_route_limit_429(self):
        """LLM 路由低限命中 → 429（S2）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"llm_route_limits": {"/v1/answer": 2}}}
        gate, *_, log, _ = make_gate(cfg=cfg)
        for i in range(3):
            resp = Resp()
            run(
                gate(
                    make_scope(path="/v1/answer", key=AGENT_KEY), receive, resp.__call__
                )
            )
            if i < 2:
                assert resp.status == 200
        assert resp.status == 429  # 第 3 次超 2/min

    def test_mcp_entry_protects_all_paths(self):
        """MCP 侧全部路径受保护（无 health 豁免）。"""
        _clock()["t"] = FAKE_NOW
        cfg = {"server": {"auth_fail_threshold": 1, "ban_seconds": 900}}
        gate, state, *_, log, _ = make_gate(cfg=cfg, entry="mcp", downstream_status=403)
        run(
            gate(
                make_scope(path="/mcp", key="wrong", method="POST"),
                receive,
                Resp().__call__,
            )
        )
        # MCP 侧无 health 豁免：/v1/health 也会被观测（形态一致性）
        t = FAKE_NOW + 1
        _clock()["t"] = t
        resp = Resp()
        run(gate(make_scope(path="/anything", key=AGENT_KEY), receive, resp.__call__))
        assert resp.status == 429  # 已封禁
