"""Per-host async rate limiting (NFR-01).

Each host gets a leaky bucket (:class:`aiolimiter.AsyncLimiter`). After an HTTP 429 the
host's rate is halved for 60 s, then restored. Penalties stack down to a floor of
``min_rps`` and each new 429 restarts the 60 s window.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from aiolimiter import AsyncLimiter

PENALTY_SECONDS = 60.0


class HostRateLimiter:
    """Leaky-bucket limiter for one host, in requests per second."""

    def __init__(
        self,
        rps: float,
        *,
        min_rps: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rps <= 0:
            raise ValueError("rps must be positive")
        self.base_rps = float(rps)
        self.min_rps = min(float(min_rps), self.base_rps)
        self._clock = clock
        self._current_rps = self.base_rps
        self._penalty_until = 0.0
        self._limiter = self._make(self.base_rps)

    @staticmethod
    def _make(rps: float) -> AsyncLimiter:
        # One token per 1/rps seconds; a bucket of 1 keeps the rate smooth (no bursts).
        return AsyncLimiter(1, 1.0 / rps)

    @property
    def current_rps(self) -> float:
        """Rate in force now (requests per second)."""
        self._maybe_restore()
        return self._current_rps

    def _maybe_restore(self) -> None:
        if self._current_rps != self.base_rps and self._clock() >= self._penalty_until:
            self._current_rps = self.base_rps
            self._limiter = self._make(self.base_rps)

    def penalize(self) -> None:
        """Halve the rate for :data:`PENALTY_SECONDS` after a 429."""
        self._maybe_restore()
        self._current_rps = max(self.min_rps, self._current_rps / 2.0)
        self._penalty_until = self._clock() + PENALTY_SECONDS
        self._limiter = self._make(self._current_rps)

    async def acquire(self) -> None:
        """Wait until a request may be sent."""
        self._maybe_restore()
        await self._limiter.acquire()


class RateLimiterRegistry:
    """One :class:`HostRateLimiter` per host, created on first use."""

    def __init__(self, default_rps: float = 5.0, per_host: dict[str, float] | None = None) -> None:
        self.default_rps = default_rps
        self.per_host = dict(per_host or {})
        self._limiters: dict[str, HostRateLimiter] = {}

    def for_host(self, host: str) -> HostRateLimiter:
        """Return the limiter for ``host``."""
        key = host.lower()
        if key not in self._limiters:
            self._limiters[key] = HostRateLimiter(self.per_host.get(key, self.default_rps))
        return self._limiters[key]
