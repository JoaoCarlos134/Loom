"""Async rate limiting and retry-with-backoff.

Public APIs like Jikan publish two simultaneous limits (a burst-per-second and
a sustained-per-minute). A single token bucket cannot express both, so
:class:`RateLimiter` runs one sliding window per limit and waits for whichever
is currently binding.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque

from recsys.log import get_logger

log = get_logger(__name__)


class RateLimiter:
    """Sliding-window limiter enforcing several (n requests / window) rules.

    Not merely "sleep 1/rate": that under-uses the budget after an idle gap and
    silently over-runs the per-minute rule during a long paginated crawl.
    """

    def __init__(self, per_sec: float, per_min: float) -> None:
        # (max_events, window_seconds, timestamps)
        self._windows: list[tuple[float, float, deque[float]]] = []
        if per_sec > 0:
            self._windows.append((per_sec, 1.0, deque()))
        if per_min > 0:
            self._windows.append((per_min, 60.0, deque()))
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until issuing one request keeps every window within budget."""
        async with self._lock:
            while True:
                now = time.monotonic()
                wait = 0.0
                for limit, window, stamps in self._windows:
                    while stamps and now - stamps[0] >= window:
                        stamps.popleft()
                    if len(stamps) >= limit:
                        wait = max(wait, window - (now - stamps[0]))
                if wait <= 0:
                    for _, _, stamps in self._windows:
                        stamps.append(now)
                    return
                # Small jitter keeps concurrent ingesters from re-colliding.
                await asyncio.sleep(wait + random.uniform(0.01, 0.05))


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter. ``attempt`` is 0-based."""
    return random.uniform(0.0, min(cap, base * (2**attempt)))


def parse_retry_after(value: str | None) -> float | None:
    """Read a Retry-After header expressed in seconds. Ignores HTTP-date form."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None
