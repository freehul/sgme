"""llm/throttle.py：LLM 调用层节流器（令牌桶）。

ST-23⑥（2026-08-11 Trae 全量提炼实测撞 429）：批量提炼时平滑请求速率，
防连环撞限流——即使退避重试做得再好，批量连发本身就会触发限流。

设计：
- 令牌桶：容量 = burst，按 rate（rps）速率补充；acquire 阻塞直到拿到令牌
- 默认 rps=0.5（≈30 req/min，取常用云端限流 60 req/min 的一半作安全余量）、
  burst=1（无突发，严格平滑）——单用户提炼场景吞吐足够（20 文件 ≈ 40s）
- 时钟/休眠函数可注入：生产用 time.monotonic/time.sleep，测试用假时钟
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """令牌桶限流器（线程安全）。

    - acquire(tokens) 阻塞直到桶内令牌足够，返回实际等待秒数（0 = 立即可用）
    - 令牌按 rate/秒 补充，上限 capacity（burst）
    - 等待结束后允许轻微透支（下限 -capacity），由后续补充恢复，
      防止并发唤醒瞬间突破速率（单用户场景影响可忽略）
    """

    def __init__(
        self,
        rate: float,
        capacity: float,
        now=None,
        sleeper=None,
    ):
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate/capacity 必须为正数")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated: float | None = None
        self._lock = threading.Lock()
        self._now = now or time.monotonic
        self._sleep = sleeper or time.sleep

    def _refill(self) -> None:
        """按经过时间补充令牌（调用方须持锁）。"""
        now = self._now()
        if self._updated is None:
            self._updated = now
            return
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    def acquire(self, tokens: float = 1.0) -> float:
        """阻塞获取 tokens 个令牌，返回等待秒数。"""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            wait = (tokens - self._tokens) / self.rate
        if wait > 0:
            self._sleep(wait)
        with self._lock:
            self._refill()
            # 允许轻微透支（并发唤醒时），下限 -capacity 防无限负债
            self._tokens = max(self._tokens - tokens, -self.capacity)
            return wait
