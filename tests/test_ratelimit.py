"""The rate limiter, backoff and Retry-After parsing in ingest/ratelimit.py.

The limiter's whole job is timing, so testing it against the wall clock would be
slow and flaky. Instead its ``time``/``asyncio``/``random`` module references are
swapped for a fake clock whose ``sleep`` advances virtual time instead of
waiting -- the limiter's own logic runs unchanged, but a "wait 60 seconds"
resolves instantly and is asserted exactly.
"""

from __future__ import annotations

import asyncio

import pytest

import recsys.ingest.ratelimit as rl
from recsys.ingest.ratelimit import RateLimiter, backoff_delay, parse_retry_after


class FakeClock:
    """A monotonic clock whose ``sleep`` jumps the clock forward, recording each
    requested delay rather than blocking on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class _FakeTime:
    def __init__(self, clock: FakeClock) -> None:
        self.monotonic = clock.monotonic


class _FakeAsyncio:
    Lock = asyncio.Lock  # the real lock; only sleep is faked

    def __init__(self, clock: FakeClock) -> None:
        self.sleep = clock.sleep


class _FakeRandom:
    @staticmethod
    def uniform(a: float, b: float) -> float:
        return 0.0  # no jitter, so recorded sleeps equal the pure computed wait


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(rl, "time", _FakeTime(c))
    monkeypatch.setattr(rl, "asyncio", _FakeAsyncio(c))
    monkeypatch.setattr(rl, "random", _FakeRandom())
    return c


# --------------------------------------------------------------------------
# RateLimiter
# --------------------------------------------------------------------------


async def test_allows_a_burst_up_to_the_limit_without_waiting(clock: FakeClock) -> None:
    limiter = RateLimiter(per_sec=3, per_min=0)
    for _ in range(3):
        await limiter.acquire()
    assert clock.sleeps == []  # three fit in the first second


async def test_blocks_at_the_limit_and_waits_out_one_window(clock: FakeClock) -> None:
    limiter = RateLimiter(per_sec=3, per_min=0)
    for _ in range(3):
        await limiter.acquire()  # fill the second at t=0

    await limiter.acquire()  # the fourth must wait for the oldest to age out
    assert clock.sleeps == pytest.approx([1.0])
    assert clock.now == pytest.approx(1.0)


async def test_the_per_minute_window_binds_over_the_per_second_one(clock: FakeClock) -> None:
    # per_sec is effectively unlimited here; the minute budget is what bites.
    limiter = RateLimiter(per_sec=1000, per_min=3)
    for _ in range(3):
        await limiter.acquire()

    await limiter.acquire()
    # The fourth waits ~60s for the first of the three to leave the minute window.
    assert clock.sleeps == pytest.approx([60.0])


async def test_budget_refills_once_a_window_has_passed(clock: FakeClock) -> None:
    limiter = RateLimiter(per_sec=3, per_min=0)
    for _ in range(3):
        await limiter.acquire()
    await limiter.acquire()  # forces a 1.0s wait, clock -> 1.0

    before = list(clock.sleeps)
    # At t=1.0 the original three have expired, so two more must not wait.
    await limiter.acquire()
    await limiter.acquire()
    assert clock.sleeps == before  # no new sleeps


async def test_a_zero_limit_window_is_ignored(clock: FakeClock) -> None:
    # per_min disabled -> only the per_sec rule exists; many calls, one window.
    limiter = RateLimiter(per_sec=2, per_min=0)
    assert len(limiter._windows) == 1
    await limiter.acquire()
    await limiter.acquire()
    assert clock.sleeps == []


# --------------------------------------------------------------------------
# backoff_delay
# --------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", [0, 1, 2, 3, 5, 10])
def test_backoff_stays_within_full_jitter_bounds(attempt: int) -> None:
    ceiling = min(60.0, 1.0 * (2**attempt))
    for _ in range(200):
        d = backoff_delay(attempt)
        assert 0.0 <= d <= ceiling


def test_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    # random.uniform(0, hi) returns hi -> observe the ceiling directly.
    monkeypatch.setattr(rl.random, "uniform", lambda a, b: b)
    assert backoff_delay(50, base=1.0, cap=60.0) == 60.0
    assert backoff_delay(3, base=2.0, cap=60.0) == pytest.approx(16.0)  # 2 * 2**3


def test_backoff_attempt_zero_spans_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl.random, "uniform", lambda a, b: b)
    assert backoff_delay(0, base=1.5) == pytest.approx(1.5)


# --------------------------------------------------------------------------
# parse_retry_after
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5", 5.0),
        ("0", 0.0),
        ("2.5", 2.5),
        ("  7 ", 7.0),
        ("-3", 0.0),  # a negative wait is clamped, never negative
    ],
)
def test_parse_retry_after_reads_seconds(value: str, expected: float) -> None:
    assert parse_retry_after(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", [None, "", "   ", "Wed, 21 Oct 2015 07:28:00 GMT", "soon"])
def test_parse_retry_after_ignores_non_numeric(value: str | None) -> None:
    assert parse_retry_after(value) is None
