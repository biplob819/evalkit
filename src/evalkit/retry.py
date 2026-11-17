"""Retry policy for provider calls (ADP-06).

Retried: connection errors, timeouts, HTTP 408, 409, 429 and 5xx, up to 6 attempts with
exponential backoff and full jitter. Never retried: any other 4xx (a bad request stays bad)
and schema failures, which are not transport errors at all (B1). Provider SDK clients are
created with `max_retries=0` so this policy is the only one in force and attempts are counted.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

__all__ = ["with_retries"]  # public since 0.3.0

T = TypeVar("T")
RETRY_STATUS = frozenset({408, 409, 429})
TRANSIENT_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectionError",
        "TimeoutError",
        "InternalServerError",
        "RateLimitError",
        "OverloadedError",
    }
)


def is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in RETRY_STATUS or status >= 500
    if isinstance(exc, asyncio.TimeoutError | ConnectionError | TimeoutError):
        return True
    return any(cls.__name__ in TRANSIENT_NAMES for cls in type(exc).__mro__)


def backoff_delays(attempts: int, base: float, cap: float, rng: random.Random) -> list[float]:
    """Full-jitter exponential backoff: delay_i ~ U(0, min(cap, base * 2**i))."""
    return [rng.uniform(0, min(cap, base * 2**i)) for i in range(attempts - 1)]


async def with_retries(
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = 6,
    base: float = 0.5,
    cap: float = 30.0,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    delays = backoff_delays(attempts, base, cap, rng or random.Random())
    for attempt in range(attempts):
        try:
            return await call()
        except Exception as exc:
            if attempt == attempts - 1 or not is_retryable(exc):
                raise
            await sleep(delays[attempt])
    raise AssertionError("unreachable")  # pragma: no cover
