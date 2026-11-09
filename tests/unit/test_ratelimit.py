from __future__ import annotations

import pytest

from pmr.http.ratelimit import PENALTY_SECONDS, HostRateLimiter, RateLimiterRegistry


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_penalty_halves_then_restores() -> None:
    clock = FakeClock()
    lim = HostRateLimiter(4.0, clock=clock)
    assert lim.current_rps == 4.0
    lim.penalize()
    assert lim.current_rps == 2.0
    clock.t += PENALTY_SECONDS / 2
    lim.penalize()
    assert lim.current_rps == 1.0
    clock.t += PENALTY_SECONDS - 1
    assert lim.current_rps == 1.0
    clock.t += 1.5
    assert lim.current_rps == 4.0


def test_penalty_floor() -> None:
    lim = HostRateLimiter(1.0, min_rps=0.5, clock=FakeClock())
    for _ in range(5):
        lim.penalize()
    assert lim.current_rps == 0.5


def test_invalid_rate() -> None:
    with pytest.raises(ValueError):
        HostRateLimiter(0)


async def test_acquire_and_registry() -> None:
    reg = RateLimiterRegistry(default_rps=50.0, per_host={"a.example": 100.0})
    a = reg.for_host("A.example")
    assert a is reg.for_host("a.example")
    assert a.base_rps == 100.0
    assert reg.for_host("b.example").base_rps == 50.0
    await a.acquire()
    await a.acquire()
