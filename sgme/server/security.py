"""sgme/server/security.py：SecurityGate 纯 ASGI 双入口安全门（T-260，ST-55，方案 v3）。

单一策略源（方案 §2.2~§2.4）：封禁检查 + 无 key IP 桶 + per-key 限流 + LLM 路由低限
+ 失败计数 + 审计调用，全部收敛在本中间件；HTTP（app.py）与 MCP（mcp_server.py）
两入口装配同一实现（T-262 / T-263 接线）。

形态铁律（T-155 实测 SSE 死锁教训）：
- **纯 ASGI**（scope / receive / send 直操作），严禁 BaseHTTPMiddleware；
- 响应透传不缓冲：包装 send 只在 ``http.response.start`` 观测状态码，字节原样转发，
  SSE / 流式响应零阻塞面；
- 不 import ``server/app.py``（防循环依赖）——429 响应体自建 JSON，形态照
  ``mcp_server._send_forbidden`` 先例（D8），两入口一致。

fail-open 收窄（S8，§2.0-5）：
- try/except **只包「状态读写与审计写」**（_safe_state / _safe_log）；
- 「是否放行 / 是否封禁」的判定逻辑**不得**整体包进 try——判定异常按
  fail-open 放行仅限状态读失败这一条路径；
- 内部异常计数 + 每 10 次打一条 WARNING（让「防护长期半坏」可见）。

有界（K5）：失败计数 / 封禁表 / 升级阶梯走 SecurityState 清扫与 10 万上限；
三个限流桶（per-key / IP / 工具）实例由构造注入（T-262 创建、双 app 共享）；
ban_expired 过渡记忆为进程内存、有界（_EXPIRED_MEM_MAX）。

来源键（K3/K6/D5）：``net_guard.resolve_client_ip``（解析后来源）+ ``ip_key``
（IPv6 /64 聚合）；trusted_proxies 由 cfg 注入并按原始值缓存解析（D6）。

豁免口径（D2/D3/D9）：
- ``/v1/health``（HTTP 侧）：完全绕过本门（探针语义不得变为 429）；
- 回环来源：豁免封禁（只计数不封，K2），**不豁免**无 key IP 桶与 per-key 限流；
- 空 / 空白 X-API-Key 视同无 key（进 IP 桶）；重复头取首个。
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sgme import net_guard
from sgme.security_log import SecurityLog
from sgme.security_state import SecurityState

logger = logging.getLogger("sgme.server.security")

# 429 错误码（与 app.py ERROR_CODES 的 ERR_RATE_LIMITED 同名；不 import app 防循环依赖）
_ERR_RATE_LIMITED = "ERR_RATE_LIMITED"

# 健康探针路径（HTTP 侧完全绕过；MCP 侧无此路径）
_HEALTH_PATH = "/v1/health"

# fail-open 内部异常的 WARNING 节流：每 N 次打一条
_WARN_EVERY = 10

# ban_expired 过渡记忆上限（进程内存、重启清空；超限丢最旧）
_EXPIRED_MEM_MAX = 10_000

# trusted_proxies 解析缓存上限（按原始值缓存，D6；热更新换值自然产生新键）
_PROXY_CACHE_MAX = 32
_PROXY_CACHE: OrderedDict[tuple[str, ...], list[Any]] = OrderedDict()


def _cached_trusted_proxies(raw_items: list[str] | None) -> list[Any]:
    """trusted_proxies 配置 → 网络对象列表（按原始值缓存，D6；解析失败项已被过滤）。"""
    key = tuple(str(x).strip() for x in (raw_items or []))
    cached = _PROXY_CACHE.get(key)
    if cached is not None:
        _PROXY_CACHE.move_to_end(key)
        return cached
    networks = net_guard.parse_trusted_proxies(list(key))
    _PROXY_CACHE[key] = networks
    _PROXY_CACHE.move_to_end(key)
    while len(_PROXY_CACHE) > _PROXY_CACHE_MAX:
        _PROXY_CACHE.popitem(last=False)
    return networks


class SecurityGate:
    """双入口安全门（纯 ASGI）。全部依赖构造注入（可测、可重置、两入口共享）。

    Args:
        app: 下游 ASGI 应用。
        state: SecurityState 实例（失败计数 / 封禁 / 升级阶梯）。
        ip_limiter: 无 key IP 桶（SlidingWindowRateLimiter，T-262 创建共享）。
        key_limiter: per-key 桶（同上；按 X-API-Key 取值计桶，含无效 key，D1）。
        tool_limiter: MCP 工具低限桶（共享实例；本门不直接消费——工具名在请求体里，
            由 T-263 在 ApiKeyMiddleware 的 body 重放点接线，此处注入仅为共享同一实例）。
        cfg_getter: 返回 cfg dict 的 callable（每请求读取阈值，对齐 ratelimit._resolve_limit
            热更新形态；MCP 侧传 lambda: _app_state.get("cfg")）。
        log_dir: 审计日志目录（$SGME_HOME/logs）；log 未注入时内部自建 SecurityLog。
        entry: "http" | "mcp"——决定受保护路径判定（HTTP 排除 /v1/health；MCP 全部）。
        log: 可选 SecurityLog 注入（测试用；缺省自建）。
        clock: 可选 float 时钟（测试用；缺省 time.time）。
    """

    def __init__(
        self,
        app: Any,
        *,
        state: SecurityState,
        ip_limiter: Any,
        key_limiter: Any,
        tool_limiter: Any,
        cfg_getter: Callable[[], dict | None],
        log_dir: str,
        entry: str = "http",
        log: SecurityLog | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.app = app
        self._state = state
        self._ip_limiter = ip_limiter
        self._key_limiter = key_limiter
        self._tool_limiter = (
            tool_limiter  # 共享注入；消费点在 MCP ApiKeyMiddleware（T-263）
        )
        self._cfg_getter = cfg_getter
        self._entry = entry
        self._clock = clock or time.time
        self._log = log  # 延迟自建（要读 cfg 的 keep_days，放首请求）
        self._log_dir = log_dir
        # fail-open 观测：内部异常计数 + 周期性 WARNING（S8）
        self._internal_errors = 0
        # ban_expired 过渡记忆：key_src -> 上次封禁到期时刻（有界）
        self._prev_bans: OrderedDict[str, float] = OrderedDict()

    # ------------------------------------------------------------------
    # fail-open 收窄包装（S8：try 只包状态/审计，不包判定逻辑）
    # ------------------------------------------------------------------

    def _note_internal_error(self, where: str, exc: Exception) -> None:
        self._internal_errors += 1
        if self._internal_errors % _WARN_EVERY == 1:
            logger.warning(
                "SecurityGate 内部异常（fail-open 放行，累计 %s 次）[%s]: %s",
                self._internal_errors,
                where,
                exc,
            )

    @staticmethod
    def _now_dt(now: float) -> datetime:
        """float 时钟 → aware datetime（审计事件时间戳与 Gate 时钟同源，可测）。"""
        return datetime.fromtimestamp(now, tz=UTC)

    def _safe_state(self, where: str, fn: Callable[[], Any], default: Any) -> Any:
        """状态读写包装：异常 → 记数 + WARNING（节流），返回 fail-open 缺省值。"""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001——fail-open 收窄面（S8）
            self._note_internal_error(where, exc)
            return default

    def _safe_log(self, where: str, fn: Callable[[], Any]) -> None:
        """审计写包装：异常只 WARNING（审计是旁路，§2.0-5）。"""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self._note_internal_error(where, exc)

    def _get_log(self) -> SecurityLog | None:
        if self._log is None and self._log_dir:
            keep_days = 0
            try:
                keep_days = int(
                    (self._cfg_getter() or {})
                    .get("server", {})
                    .get("security_log_keep_days", 0)
                    or 0
                )
            except Exception:  # noqa: BLE001——配置读失败按 0（不清理）
                keep_days = 0
            self._log = SecurityLog(self._log_dir, keep_days=keep_days)
        return self._log

    # ------------------------------------------------------------------
    # 配置读取（每请求读，对齐 _resolve_limit 先例；异常回缺省）
    # ------------------------------------------------------------------

    def _server_cfg(self) -> dict:
        try:
            cfg = self._cfg_getter() or {}
            server = cfg.get("server")
            return server if isinstance(server, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _int_opt(server: dict, key: str, default: int) -> int:
        try:
            value = int(server.get(key, default))
            return value if value >= 0 else default
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # ASGI 入口
    # ------------------------------------------------------------------

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # D9：health 探针完全绕过（HTTP 侧；语义不得变为 429）
        if self._entry == "http" and path == _HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        # D2：重复头取首个；空串 / 纯空白视同无 key
        presented: str | None = None
        xff: str | None = None
        ua: str = ""
        for h_name, h_value in headers:
            if h_name == b"x-api-key" and presented is None:
                presented = h_value.decode("latin-1")
            elif h_name == b"x-forwarded-for" and xff is None:
                xff = h_value.decode("latin-1")
            elif h_name == b"user-agent" and not ua:
                ua = h_value.decode("latin-1")[:200]
        key_present = bool(presented and presented.strip())

        server = self._server_cfg()
        now = self._clock()

        # ---- 来源解析（K3 消费点 1）+ 回环判定（消费点 4）----
        client = scope.get("client")
        peer = client[0] if client else ""
        proxies = _cached_trusted_proxies(server.get("trusted_proxies") or [])
        source = net_guard.resolve_client_ip(peer, xff, proxies)
        src_key = net_guard.ip_key(source)
        loopback = net_guard.is_loopback_source(source)
        protected = True  # MCP 全部路径受保护；HTTP 已排除 health

        # ---- 封禁检查（§2.2；先于一切限流，禁期全阻断含有效 key）----
        threshold = self._int_opt(server, "auth_fail_threshold", 0)
        if loopback:
            banned, remaining, _level = False, 0, 0  # K2：回环豁免封禁（只计数不封）
        else:
            banned, remaining, _level = self._safe_state(
                "is_banned",
                lambda: self._state.is_banned(src_key, now),
                (False, 0, 0),
            )
        self._track_ban_transition(src_key, banned, now, remaining)
        if banned:
            await self._send_429(send, remaining)
            return

        # ---- 限流：per-key（D1 含无效 key 计桶）/ 无 key IP 桶（§2.3）----
        if key_present:
            allowed, retry = self._safe_state(
                "key_limit",
                lambda: self._key_limiter.is_allowed(presented),
                (True, 0.0),
            )
            if not allowed:
                self._safe_log(
                    "rate_limited",
                    lambda: self._get_log().record_rate_limited(
                        net_guard.mask_key(presented),
                        scope="key",
                        limit=self._int_opt(server, "rate_limit_per_min", 120),
                        user_agent=ua,
                        now=self._now_dt(now),
                    ),
                )
                await self._send_429(send, int(retry) + 1)
                return
            # ---- LLM 路由低限（S2，HTTP 侧；MCP 工具低限在 ApiKeyMiddleware，T-263）----
            if self._entry == "http":
                route_retry = self._check_llm_route_limit(path, presented, server)
                if route_retry is not None:
                    self._safe_log(
                        "llm_route_limited",
                        lambda: self._get_log().record_rate_limited(
                            net_guard.mask_key(presented),
                            scope="llm_route",
                            limit=route_retry[1],
                            user_agent=ua,
                            now=self._now_dt(now),
                        ),
                    )
                    await self._send_429(send, route_retry[0])
                    return
        else:
            allowed, retry = self._safe_state(
                "ip_limit", lambda: self._ip_limiter.is_allowed(src_key), (True, 0.0)
            )
            if not allowed:
                self._safe_log(
                    "ip_limited",
                    lambda: self._get_log().record_ip_limited(
                        src_key,
                        limit=self._int_opt(server, "ip_rate_limit_per_min", 60),
                        user_agent=ua,
                        now=self._now_dt(now),
                    ),
                )
                await self._send_429(send, int(retry) + 1)
                return

        # ---- 放行 + 经包装 send 观测 401/403 失败（§2.2 消费点）----
        status_holder: dict[str, int] = {"status": 0}

        async def observing_send(message) -> None:
            if (
                message["type"] == "http.response.start"
                and status_holder["status"] == 0
            ):
                status_holder["status"] = int(message.get("status", 0))
            await send(message)

        await self.app(scope, receive, observing_send)

        if protected and status_holder["status"] in (401, 403):
            self._count_failure(
                src_key=src_key,
                presented=presented,
                loopback=loopback,
                threshold=threshold,
                server=server,
                now=now,
                ua=ua,
            )

    # ------------------------------------------------------------------
    # 失败计数与封禁触发（K1：升级由 SecurityState 唯一实现）
    # ------------------------------------------------------------------

    def _count_failure(
        self,
        *,
        src_key: str,
        presented: str | None,
        loopback: bool,
        threshold: int,
        server: dict,
        now: float,
        ua: str,
    ) -> None:
        # 回环失败只计数不封（D3/K2）：对状态机传 threshold=0
        effective_threshold = 0 if loopback else threshold
        decision = self._safe_state(
            "record_failure",
            lambda: self._state.record_failure(
                src_key,
                now,
                effective_threshold,
                self._int_opt(server, "auth_fail_window_sec", 300),
                self._int_opt(server, "ban_seconds", 900),
                self._int_opt(server, "ban_max_seconds", 3600),
                self._int_opt(server, "ban_reset_sec", 86400),
            ),
            None,
        )
        # 审计：回环失败由 SecurityLog 内部仅计数不落盘（K4）
        self._safe_log(
            "auth_fail",
            lambda: self._get_log().record_auth_fail(
                src_key,
                key=presented,
                reason="auth rejected by entry",
                user_agent=ua,
                threshold=threshold,
                is_loopback=loopback,
                now=self._now_dt(now),
            ),
        )
        if decision and decision.get("banned"):
            duration = int(decision.get("duration", 0))
            escalated = bool(decision.get("escalated"))
            # 封禁触发即登记到期时刻：到期后首次请求记 ban_expired（§2.5 过渡事件）
            self._prev_bans[src_key] = now + max(1, duration)
            self._prev_bans.move_to_end(src_key)
            self._safe_log(
                "banned",
                lambda: self._get_log().record_banned(
                    src_key,
                    duration_sec=duration,
                    escalated=escalated,
                    now=self._now_dt(now),
                ),
            )
            if escalated:
                self._safe_log(
                    "ban_escalated",
                    lambda: self._get_log().record_ban_escalated(
                        src_key,
                        prev_duration_sec=max(1, duration // 2),
                        new_duration_sec=duration,
                        now=self._now_dt(now),
                    ),
                )

    # ------------------------------------------------------------------
    # ban_expired 过渡（§2.5 状态迁移事件；每过渡一条，有界记忆）
    # ------------------------------------------------------------------

    def _track_ban_transition(
        self, src_key: str, banned: bool, now: float, remaining: int
    ) -> None:
        if banned:
            # 登记「封禁中」：到期后第一次出现解除态时记 ban_expired（§2.5 过渡事件）
            self._prev_bans[src_key] = now + max(1, remaining)
            self._prev_bans.move_to_end(src_key)
            return
        prev_until = self._prev_bans.pop(src_key, None)
        if prev_until is not None and now >= prev_until:
            # 封禁刚到期 → ban_expired 记一条（过期的后续请求不再重复记）
            self._safe_log(
                "ban_expired",
                lambda: self._get_log().record_ban_expired(
                    src_key, now=self._now_dt(now)
                ),
            )
        while len(self._prev_bans) > _EXPIRED_MEM_MAX:
            self._prev_bans.popitem(last=False)

    # ------------------------------------------------------------------
    # LLM 路由低限（S2，HTTP 侧）：llm_route_limits 前缀匹配、按 key 分钟限
    # ------------------------------------------------------------------

    def _check_llm_route_limit(
        self, path: str, presented: str, server: dict
    ) -> tuple[int, int] | None:
        """命中返回 (retry_after_sec, limit)；未命中 None。桶按路由惰性创建（共享底座）。"""
        limits = server.get("llm_route_limits")
        if not isinstance(limits, dict) or not limits:
            return None
        matched: tuple[str, int] | None = None
        for route, limit in limits.items():
            r = str(route)
            if path == r or path.startswith(r.rstrip("/") + "/"):
                if matched is None or len(r) > len(matched[0]):
                    try:
                        matched = (r, int(limit))
                    except (TypeError, ValueError):
                        continue
        if matched is None:
            return None
        route, limit = matched
        if limit <= 0:
            return None
        limiter = self._route_limiters().get(route, limit)
        allowed, retry = self._safe_state(
            "llm_route_limit", lambda: limiter.is_allowed(presented), (True, 0.0)
        )
        if allowed:
            return None
        return max(1, int(retry)), limit

    def _route_limiters(self) -> dict:
        """路由 → SlidingWindowRateLimiter 惰性表（路由数有限，来自配置）。"""
        cache = getattr(self, "_route_limiter_cache", None)
        if cache is None:
            cache = _RouteLimiterCache(self)
            self._route_limiter_cache = cache
        return cache

    # ------------------------------------------------------------------
    # 429 响应（D8：自建 JSON，照 _send_forbidden 先例；两入口形态一致）
    # ------------------------------------------------------------------

    @staticmethod
    async def _send_429(send, retry_after: int) -> None:
        retry_after = max(1, int(retry_after))
        body = json.dumps(
            {
                "error": {
                    "code": _ERR_RATE_LIMITED,
                    "message": f"请求过于频繁或来源已被临时封禁，请 {retry_after} 秒后重试",
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"retry-after", str(retry_after).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _RouteLimiterCache:
    """llm_route_limits 的路由桶表：按配置惰性建 SlidingWindowRateLimiter。

    路由键来自配置（有限集）；limit 热更新时按 (route, limit) 重建。
    桶底座与 per-key / IP 桶同款（T-264 统一加清扫与上限）。
    """

    def __init__(self, gate: SecurityGate) -> None:
        self._gate = gate
        self._buckets: OrderedDict[tuple[str, int], Any] = OrderedDict()

    def get(self, route: str, limit: int) -> Any:
        from sgme.server.ratelimit import SlidingWindowRateLimiter

        key = (route, limit)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = SlidingWindowRateLimiter(limit_per_min=limit)
            self._buckets[key] = bucket
            while len(self._buckets) > 64:  # 路由集有限；防配置翻新堆积
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        return bucket
