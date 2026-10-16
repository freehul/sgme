"""ST-58/T-288：`mask_key` 三处引用修复（P7）。

背景（2026-10-16 审计 + DS 审查复核）：
- `sgme/server/security.py` 两处用 `net_guard.mask_key(...)`，但 `sgme/net_guard.py`
  **没有**该函数 → `AttributeError` 被 `_safe_log` 吞掉，只留一条 WARNING，
  **安全审计记录静默丢失**；
- `sgme/mcp_server.py` 一处裸 `mask_key(key)`，从未导入 → `NameError` 同样被
  内层 `except Exception: pass` 吞掉。

⚠️ 定性修正（不要按 fail-open 理解）：`_send_429` 在 `_safe_log` / `except` **之外**
（`security.py:283`、`mcp_server.py:349`），所以**限流与 429 一直生效**。
本条修的是「审计落盘丢失」，不是「限流被绕过」。测试据此只断言审计事件。

唯一实现：`sgme/security_log.py::mask_key`。修复方式是**改引用点**，不在
`net_guard` 加转发别名（防两处实现漂移）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sgme.security_log import SecurityLog, mask_key
from sgme.server import security as sec_mod
from sgme import mcp_server as mcp_mod


# ---------- 1. 引用点可解析（防回归：任何人再写错名字都会被这里拦住） ----------


def test_security_module_resolves_mask_key():
    """server.security 内的 `mask_key` 必须能解析，且就是 security_log 的实现。"""
    fn = getattr(sec_mod, "mask_key", None)
    assert fn is not None, "server.security 缺少 mask_key（回退到 net_guard 的错误引用）"
    assert fn is mask_key


def test_mcp_server_resolves_mask_key():
    """mcp_server 内的 `mask_key` 必须能解析（修复前是裸名 → NameError）。"""
    fn = getattr(mcp_mod, "mask_key", None)
    assert fn is not None, "mcp_server 缺少 mask_key"
    assert fn is mask_key


def test_net_guard_has_no_mask_key_alias():
    """不得在 net_guard 加转发别名（两处实现漂移防线）。

    若将来有人在 net_guard 补了 `mask_key`，本用例会失败并迫使重新讨论——
    说明「唯一实现」约束被打破了。
    """
    from sgme import net_guard

    assert not hasattr(net_guard, "mask_key"), (
        "net_guard 不应持有 mask_key 别名；唯一实现固定在 sgme.security_log"
    )


def test_mask_key_semantics():
    """脱敏指纹：前 6 + … + 后 2；≤8 字符整体隐藏；空值空串。"""
    assert mask_key("sk-abcdefghijklmnop") == "sk-abc…op"
    assert mask_key("short") == "…"
    assert mask_key(None) == ""
    assert mask_key("") == ""


# ---------- 2. 端到端：构造限流 → 审计事件落盘（G6 1/1） ----------


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Resp:
    def __init__(self):
        self.status = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


def _make_scope(key: str, path: str = "/v1/answer"):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"x-api-key", key.encode("latin-1")),
            (b"user-agent", b"mask-key-ref-test/1.0"),
        ],
        "client": ("203.0.113.5", 12345),
        "server": ("testserver", 80),
    }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def test_rate_limited_event_is_recorded(tmp_path: Path):
    """构造一次真实限流 → `security-YYYYMMDD.jsonl` 必须出现 `rate_limited` 事件。

    这是 T-288 的核心验收（G6 1/1）：修复前该事件因 mask_key NameError 而丢失。
    """
    from sgme.server.ratelimit import SlidingWindowRateLimiter
    from sgme.server.security import SecurityGate
    from sgme.security_state import SecurityState

    log_dir = tmp_path / "seclog"
    log_dir.mkdir()
    cfg = {"server": {"rate_limit_per_min": 2}}

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    gate = SecurityGate(
        downstream,
        state=SecurityState(),
        ip_limiter=SlidingWindowRateLimiter(limit_per_min=100),
        key_limiter=SlidingWindowRateLimiter(limit_per_min=2),
        tool_limiter=SlidingWindowRateLimiter(limit_per_min=100),
        cfg_getter=lambda: cfg,
        log_dir=str(log_dir),
        entry="http",
    )

    key = "test-rate-limited-key-0001"  # 数据卫生：低熵假值
    statuses = []
    for _ in range(4):
        resp = _Resp()
        _run(gate(_make_scope(key), _receive, resp.__call__))
        statuses.append(resp.status)

    # 限流确实触发（证明 _send_429 一直在生效，本条不是修限流）
    assert 429 in statuses, f"预期出现 429，实际 {statuses}"

    date = datetime.now(UTC).strftime("%Y-%m-%d")
    events = SecurityLog(log_dir, keep_days=0).read_events(date, limit=100)
    kinds = [e.get("event") or e.get("kind") or e.get("type") for e in events]
    assert "rate_limited" in kinds, (
        f"审计日志缺少 rate_limited 事件（说明 mask_key 引用又坏了）；事件={kinds}"
    )


def test_rate_limited_event_masked_source(tmp_path: Path):
    """落盘事件的 source 必须是脱敏指纹，不得出现明文 key。"""
    log = SecurityLog(tmp_path, keep_days=0)
    key = "test-plain-key-should-not-appear"
    ok = log.record_rate_limited(mask_key(key), scope="key", limit=2,
                                 now=datetime.now(UTC))
    assert ok is True
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    events = log.read_events(date, limit=10)
    blob = "".join(str(e) for e in events)
    assert key not in blob, "审计事件泄漏明文 key"
    assert mask_key(key) in blob
