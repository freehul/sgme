"""sgme/server/ratelimit.py：滑动窗口限流中间件（T-7，§6 限流定稿）。

设计要点（对照 SGME-接口契约-v0.1.md §6）：
- 维度：按请求头 ``X-API-Key`` 区分调用方；无 Key 的请求不计限流（交后续鉴权返回 401/403）。
- 窗口：滑动窗口，1 分钟（60s）为统计窗口；超限即拒绝。
- 阈值：默认 **120 req/min/Key**（契约承诺）；配置 ``cfg["server"]["rate_limit_per_min"]``；
  ``0`` 表示关闭（不限制）。
- 超限响应：429 ``ERR_RATE_LIMITED`` + ``Retry-After`` 响应头（秒，向上取整）。
- 豁免：``/v1/health`` 监控探测不受影响。
- 线程安全：每 key 一个时间戳 ``deque``，全局 ``threading.Lock`` 保护读写。

读取配置方式：中间件在每次请求时从 ``request.app.state.cfg``（即 ``create_app(cfg=...)``
注入的配置）读取 ``server.rate_limit_per_min``；测试可通过注入小值 cfg 验证。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict, deque
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from sgme.server.app import ERROR_CODES, error_response

logger = logging.getLogger("sgme.server.ratelimit")

# 默认阈值（契约承诺：120 req/min/Key）
DEFAULT_RATE_LIMIT_PER_MIN: int = 120
# 滑动窗口长度（秒）
WINDOW_SECONDS: int = 60
# 豁免路径（监控探测）
HEALTH_PATH: str = "/v1/health"


class SlidingWindowRateLimiter:
    """按 key 的滑动窗口限流器（线程安全）。

    每个 key 维护一个时间戳 ``deque``，请求到达时：
    1. 丢弃窗口外（> WINDOW_SECONDS）的旧时间戳；
    2. 若窗口内计数 >= limit → 拒绝，并返回最早一条离开窗口所需秒数（Retry-After）；
    3. 否则追加当前时间戳并放行。

    ``limit <= 0`` 视为关闭（始终放行）。
    """

    def __init__(self, limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN, window_seconds: int = WINDOW_SECONDS) -> None:
        self.limit = max(0, int(limit_per_min))
        self.window = max(1, int(window_seconds))
        self._buckets: dict[str, "deque[float]"] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> tuple[bool, float]:
        """记录一次请求，返回 ``(是否放行, Retry-After 秒)``。

        Args:
            key: 限流维度键（X-API-Key 明文或其脱敏标识）。

        Returns:
            ``(True, 0.0)`` 放行；
            ``(False, seconds)`` 拒绝，seconds 为建议重试等待秒数（>= 0）。
        """
        if self.limit <= 0:
            return True, 0.0
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._buckets[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.limit:
                # 拒绝：最早一条何时离开窗口 → Retry-After（向上取整秒）
                retry_after = dq[0] + self.window - now
                return False, max(0.0, retry_after)
            dq.append(now)
            return True, 0.0

    def clear(self) -> None:
        """清空全部计数（测试用）。"""
        with self._lock:
            self._buckets.clear()


def _resolve_limit(cfg: Any) -> int:
    """从配置 dict 解析限流阈值；缺失/异常/负数 → 默认 120。"""
    try:
        server_cfg = (cfg or {}).get("server")
        if not isinstance(server_cfg, dict):
            return DEFAULT_RATE_LIMIT_PER_MIN
        value = int(server_cfg.get("rate_limit_per_min", DEFAULT_RATE_LIMIT_PER_MIN))
        if value < 0:
            return DEFAULT_RATE_LIMIT_PER_MIN
        return value
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_PER_MIN


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI / Starlette 限流中间件（T-7 §6）。

    在请求进入路由前：
    - ``/v1/health`` 直接放行（豁免）；
    - 无 ``X-API-Key`` 的请求放行（交后续鉴权依赖返回 401/403）；
    - 带 Key 的请求按滑动窗口限流，超限返回 429 + Retry-After。
    """

    def __init__(self, app, dispatch=None) -> None:
        super().__init__(app, dispatch=dispatch)
        # 限流器单例（跨请求保持滑动窗口状态）；limit 每次请求从 cfg 刷新
        self._limiter = SlidingWindowRateLimiter(DEFAULT_RATE_LIMIT_PER_MIN)

    async def dispatch(self, request: Request, call_next) -> Response:
        # 刷新阈值（0=关闭）；从 app.state.cfg 读取（测试经 create_app(cfg=...) 注入）
        app_state = getattr(getattr(request, "app", None), "state", None)
        cfg = getattr(app_state, "cfg", None) if app_state is not None else None
        limit = _resolve_limit(cfg)
        self._limiter.limit = max(0, limit)

        # 健康检查豁免（监控探测不受限）
        if request.url.path == HEALTH_PATH:
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        # 无 Key → 不计限流，交后续鉴权返回 401/403
        if not api_key:
            return await call_next(request)

        allowed, retry_after = self._limiter.is_allowed(api_key)
        if not allowed:
            retry_seconds = max(1, int(math.ceil(retry_after)))
            resp = error_response(
                "ERR_RATE_LIMITED",
                "请求过于频繁，请稍后再试（已触发限流）",
                ERROR_CODES["ERR_RATE_LIMITED"],
                details={"retry_after_sec": retry_seconds},
            )
            resp.headers["Retry-After"] = str(retry_seconds)
            return resp

        return await call_next(request)
