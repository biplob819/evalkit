"""Per-provider request pacing (ADP-07): a token bucket in front of each provider.

Concurrency (how many calls at once) is enforced by the executor's per-provider semaphores
(N1.3). The token bucket adds a rate (how many calls per second), so a burst of short calls
cannot exceed a provider's requests-per-minute limit.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_s: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if rate_per_s <= 0 or capacity < 1:
            raise ValueError("rate must be > 0 and capacity >= 1")
        self.rate, self.capacity = rate_per_s, capacity
        self._clock, self._sleep = clock, sleep
        self._tokens = capacity
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens < 0 or tokens > self.capacity:
            raise ValueError(f"tokens must be between 0 and {self.capacity}, got {tokens}")
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await self._sleep((tokens - self._tokens) / self.rate)
