"""T-263 MCP 侧接线测试（I2：装配顺序锁定 + 4 工具低限 + SSE 不受阻）。

形态：直调中间件链（手搓 scope）覆盖判定逻辑；不真起 uvicorn（接线正确性由
mount_mcp 绑定 + run_mcp_server 装配顺序的代码断言与集成冒烟共同保障）。
"""

from __future__ import annotations

import json

import pytest

from sgme.security_log import SecurityLog
from sgme.security_state import SecurityState
from sgme.server.ratelimit import SlidingWindowRateLimiter
from sgme.server.security import SecurityGate
from sgme.mcp_server import ApiKeyMiddleware

FAKE_NOW = 1_760_000_000.0
ADMIN_KEY = "test-admin-key-security"
AGENT_KEY = "test-agent-key-security"


class _Clock:
    t = FAKE_NOW

    def __call__(self):
        return self.t


def make_scope(method="POST", key=AGENT_KEY, body=b"", path="/mcp"):
    headers = [(b"content-type", b"application/json")]
    if key is not None:
        headers.append((b"x-api-key", key.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "query_string": b"",
    }


async def receive_from(body: bytes):
    sent = {"done": False}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class Resp:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k: v for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


def run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


class Downstream:
    """记录收到的 body（验证重放无损）与可编程响应状态。"""

    def __init__(self, status=200):
        self.status = status
        self.bodies = []

    async def __call__(self, scope, receive, send):
        chunks = []
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        self.bodies.append(b"".join(chunks))
        await send({
            "type": "http.response.start",
            "status": self.status,
            "headers": [(b"content-type", b"text/event-stream" if self.status == 200 and scope.get("path") == "/mcp" else b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"data: ok\n\n"})


def _cfg(tool_limits=None, threshold=0):
    return {"server": {"llm_mcp_tool_limits": tool_limits or {},
                       "auth_fail_threshold": threshold}}


class TestMcpToolRateLimit:
    """4 工具低限（llm_mcp_tool_limits）。"""

    def _middleware(self, downstream, tool_limits, tmp_path):
        log = SecurityLog(tmp_path / "logs", keep_days=0)
        mw = ApiKeyMiddleware(
            downstream,
            _FakeStore(),
            conn=None,
            tool_limiter=SlidingWindowRateLimiter(limit_per_min=2),
            cfg_getter=lambda: _cfg(tool_limits=tool_limits),
            audit_log=log,
        )
        return mw, log

    def test_tool_limit_429_after_burst(self, tmp_path):
        """refine_batch 限 2/min：第 3 次 → 429 + retry-after。"""
        ds = Downstream()
        mw, log = self._middleware(ds, {"refine_batch": 2}, tmp_path)
        body = json.dumps({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "refine_batch", "arguments": {}}, "id": 1,
        }).encode()
        for i in range(3):
            resp = Resp()
            run(mw(make_scope(body=body), run(receive_from(body)), resp.__call__))
            if i < 2:
                assert resp.status == 200
        assert resp.status == 429
        assert b"retry-after" in {k for k in resp.headers}
        assert json.loads(resp.body)["error"]["code"] == "ERR_RATE_LIMITED"

    def test_body_replayed_lossless(self, tmp_path):
        """限流判定读 body 后重放无损：下游收到完整原文。"""
        ds = Downstream()
        mw, _ = self._middleware(ds, {"answer": 5}, tmp_path)
        payload = {"jsonrpc": "2.0", "method": "tools/call",
                   "params": {"name": "answer", "arguments": {"q": "x" * 500}}, "id": 2}
        body = json.dumps(payload).encode()
        resp = Resp()
        run(mw(make_scope(body=body), run(receive_from(body)), resp.__call__))
        assert resp.status == 200
        assert ds.bodies and json.loads(ds.bodies[0]) == payload

    def test_non_listed_tool_unlimited(self, tmp_path):
        """清单外工具（search）不受低限约束（本期仅 per-key 兜底）。"""
        ds = Downstream()
        mw, _ = self._middleware(ds, {"refine_batch": 1}, tmp_path)
        body = json.dumps({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "search", "arguments": {}}, "id": 3,
        }).encode()
        for _ in range(5):
            resp = Resp()
            run(mw(make_scope(body=body), run(receive_from(body)), resp.__call__))
            assert resp.status == 200

    def test_non_tools_call_unaffected(self, tmp_path):
        """非 tools/call（initialize/tools/list）不限流。"""
        ds = Downstream()
        mw, _ = self._middleware(ds, {"answer": 1}, tmp_path)
        body = json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 4}).encode()
        for _ in range(5):
            resp = Resp()
            run(mw(make_scope(body=body), run(receive_from(body)), resp.__call__))
            assert resp.status == 200


class _FakeStore:
    """最小 key_store（is_agent 放行测试 key）。"""

    def is_agent(self, key):
        return key in (AGENT_KEY, ADMIN_KEY)

    def is_admin(self, key):
        return key == ADMIN_KEY

    def is_default_dev_key(self, key):
        return False


class TestMcpGateOrdering:
    """装配顺序断言：SecurityGate 必须能看到 ApiKeyMiddleware 的 403（封禁触发链成立）。"""

    def test_gate_sees_downstream_403_counts_failure(self, tmp_path):
        """Gate 在外、ApiKey 在内：内层 403 → Gate 计失败 → 达阈值封禁。"""
        _Clock.t = FAKE_NOW
        state = SecurityState()
        cfg = _cfg(threshold=2)
        cfg_getter = lambda: cfg  # noqa: E731
        gate = SecurityGate(
            lambda scope, receive, send: _inner_403(scope, receive, send),
            state=state,
            ip_limiter=SlidingWindowRateLimiter(5),
            key_limiter=SlidingWindowRateLimiter(120),
            tool_limiter=SlidingWindowRateLimiter(5),
            cfg_getter=cfg_getter,
            log_dir=str(tmp_path / "logs"),
            entry="mcp",
            clock=_Clock(),
        )
        # 远程 peer（回环豁免封禁，K2——必须非回环才能验证封禁链）
        s1 = make_scope(key=AGENT_KEY)
        s1["client"] = ("203.0.113.5", 1)
        run(gate(s1, run(receive_from(b"")), Resp().__call__))
        s2 = make_scope(key=AGENT_KEY)
        s2["client"] = ("203.0.113.5", 2)
        run(gate(s2, run(receive_from(b"")), Resp().__call__))
        # 第三次：内层 403 之前先被 Gate 封禁拦下（429，封禁全阻断）
        _Clock.t = FAKE_NOW + 1
        s3 = make_scope(key=ADMIN_KEY)
        s3["client"] = ("203.0.113.5", 3)
        resp = Resp()
        run(gate(s3, run(receive_from(b"")), resp.__call__))
        assert resp.status == 429

    def test_shared_state_between_entries(self, tmp_path):
        """双入口共享状态：HTTP 侧封禁 → MCP 侧同样 429（同一 state 实例）。"""
        _Clock.t = FAKE_NOW
        state = SecurityState()
        cfg = _cfg(threshold=1)
        mk_gate = lambda downstream: SecurityGate(  # noqa: E731
            downstream, state=state,
            ip_limiter=SlidingWindowRateLimiter(5), key_limiter=SlidingWindowRateLimiter(120),
            tool_limiter=SlidingWindowRateLimiter(5),
            cfg_getter=lambda: cfg, log_dir=str(tmp_path / "logs"), entry="mcp",
            clock=_Clock(),
        )
        # HTTP 入口视角触发封禁（peer 远程）
        scope_http = make_scope(key="wrong")
        scope_http["client"] = ("203.0.113.5", 1)
        run(mk_gate(lambda s, r, snd: _inner_403(s, r, snd))(scope_http, receive_from(b""), Resp().__call__))
        # MCP 入口视角：同源同键 → 已封
        _Clock.t = FAKE_NOW + 5
        scope_mcp = make_scope(key=ADMIN_KEY)
        scope_mcp["client"] = ("203.0.113.5", 2)
        resp = Resp()
        run(mk_gate(lambda s, r, snd: _inner_200(s, r, snd))(scope_mcp, run(receive_from(b"")), resp.__call__))
        assert resp.status == 429


async def _inner_403(scope, receive, send):
    await send({"type": "http.response.start", "status": 403, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _inner_200(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


class TestMcpAssemblySource:
    """装配顺序源码守卫（S7 三方口径：ApiKey → 体限 → SecurityGate 最后注册）。"""

    def test_registration_order_in_source(self):
        from pathlib import Path

        src = Path("sgme/mcp_server.py").read_text(encoding="utf-8")
        # 宽松锚点：三个中间件的类名出现位置（注册代码各出现一次于 run_mcp_server）
        i_key = src.find("ApiKeyMiddleware,")
        i_body = src.find("RequestBodyLimitMiddleware, max_body_size=max_body_bytes")
        i_gate = src.find("from sgme.server.security import SecurityGate")
        assert i_key > 0 and i_body > 0 and i_gate > 0
        assert i_key < i_body < i_gate  # 注册序 = ApiKey → 体限 → Gate（Gate 最外）
