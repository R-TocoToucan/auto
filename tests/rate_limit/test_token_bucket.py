"""Tests for :class:`bithumb_bot.rate_limit.TokenBucket`.

Every test uses the ``fake_monotonic_clock`` conftest fixture and an
``AsyncMock`` sleep, so:

* The test suite spends ZERO real wall time on rate-limit delays
  (`Done` bullet: "test runtime < 1 second total").
* Every assertion about wait duration is exact — no wall-clock slop.

We use ``asyncio.run`` directly instead of adding a ``pytest-asyncio``
dependency (Ponytail + T-1-04-SC — no new packages this plan).
"""

from __future__ import annotations

import asyncio
import time as real_time
from unittest.mock import AsyncMock

import pytest

from bithumb_bot.rate_limit.token_bucket import TokenBucket
from tests.conftest import FakeMonotonicClock


def _make_sleep_mock(*, advance_clock: FakeMonotonicClock | None = None) -> AsyncMock:
    """Build an AsyncMock that records the sleep duration and optionally
    advances the fake monotonic clock by that duration.
    """

    async def _sleep(seconds: float) -> None:
        if advance_clock is not None:
            advance_clock.tick(seconds)

    return AsyncMock(side_effect=_sleep)


class TestConstruction:
    def test_starts_full(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        b = TokenBucket(capacity=2.0, refill_rate=1.0, monotonic=fake_monotonic_clock)
        assert b.tokens == 2.0
        assert b.capacity == 2.0
        assert b.refill_rate == 1.0

    def test_rejects_zero_capacity(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=0.0, refill_rate=1.0, monotonic=fake_monotonic_clock)

    def test_rejects_negative_capacity(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=-1.0, refill_rate=1.0, monotonic=fake_monotonic_clock)

    def test_rejects_zero_refill(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=1.0, refill_rate=0.0, monotonic=fake_monotonic_clock)


class TestAcquire:
    def test_first_n_acquires_within_capacity_dont_sleep(
        self, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        b = TokenBucket(capacity=2.0, refill_rate=1.0, monotonic=fake_monotonic_clock)
        sleep_mock = _make_sleep_mock()

        async def _go() -> None:
            await b.acquire(sleep_fn=sleep_mock)
            await b.acquire(sleep_fn=sleep_mock)

        asyncio.run(_go())
        assert sleep_mock.await_count == 0
        assert b.tokens == pytest.approx(0.0)

    def test_third_acquire_waits_one_second(
        self, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        b = TokenBucket(capacity=2.0, refill_rate=1.0, monotonic=fake_monotonic_clock)
        sleep_mock = _make_sleep_mock(advance_clock=fake_monotonic_clock)

        async def _go() -> None:
            await b.acquire(sleep_fn=sleep_mock)
            await b.acquire(sleep_fn=sleep_mock)
            # Third acquire: needs 1 more token at 1 token/sec -> sleep 1.0s.
            await b.acquire(sleep_fn=sleep_mock)

        asyncio.run(_go())
        assert sleep_mock.await_count == 1
        assert sleep_mock.await_args_list[0].args == (1.0,)

    def test_refill_after_clock_advance(
        self, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        b = TokenBucket(capacity=2.0, refill_rate=1.0, monotonic=fake_monotonic_clock)
        sleep_mock = _make_sleep_mock()

        async def _go() -> None:
            await b.acquire(sleep_fn=sleep_mock)
            await b.acquire(sleep_fn=sleep_mock)
            # Empty bucket. Advance 2s -> full refill (capped at capacity).
            fake_monotonic_clock.tick(2.0)
            # Two more acquires must NOT sleep.
            await b.acquire(sleep_fn=sleep_mock)
            await b.acquire(sleep_fn=sleep_mock)

        asyncio.run(_go())
        assert sleep_mock.await_count == 0

    def test_refill_caps_at_capacity(
        self, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        b = TokenBucket(capacity=2.0, refill_rate=1.0, monotonic=fake_monotonic_clock)
        sleep_mock = _make_sleep_mock()
        sleep_mock2 = _make_sleep_mock(advance_clock=fake_monotonic_clock)

        async def _go() -> None:
            await b.acquire(sleep_fn=sleep_mock)
            # Advance 100 seconds — bucket refills to `capacity`, not more.
            fake_monotonic_clock.tick(100.0)
            await b.acquire(sleep_fn=sleep_mock)
            await b.acquire(sleep_fn=sleep_mock)
            # Third acquire drains it — must sleep for 1 refill period.
            await b.acquire(sleep_fn=sleep_mock2)

        asyncio.run(_go())
        assert sleep_mock.await_count == 0
        assert sleep_mock2.await_count == 1
        assert sleep_mock2.await_args_list[0].args == (1.0,)

    def test_acquire_n_greater_than_capacity_raises(
        self, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        b = TokenBucket(capacity=2.0, refill_rate=1.0, monotonic=fake_monotonic_clock)

        async def _go() -> None:
            await b.acquire(n=3.0)

        with pytest.raises(ValueError):
            asyncio.run(_go())

    def test_acquire_zero_or_negative_raises(
        self, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        b = TokenBucket(capacity=2.0, refill_rate=1.0, monotonic=fake_monotonic_clock)

        async def _go_zero() -> None:
            await b.acquire(n=0.0)

        async def _go_neg() -> None:
            await b.acquire(n=-1.0)

        with pytest.raises(ValueError):
            asyncio.run(_go_zero())
        with pytest.raises(ValueError):
            asyncio.run(_go_neg())

    def test_acquire_multiple_tokens_waits_appropriate_time(
        self, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        """`capacity=2, refill_rate=1`: after draining, acquire(2) waits 2s."""
        b = TokenBucket(capacity=2.0, refill_rate=1.0, monotonic=fake_monotonic_clock)
        sleep_mock = _make_sleep_mock(advance_clock=fake_monotonic_clock)

        async def _go() -> None:
            await b.acquire(n=2.0, sleep_fn=sleep_mock)
            # Empty. Now acquire(2) — need 2 tokens at 1/sec -> sleep 2.0s.
            await b.acquire(n=2.0, sleep_fn=sleep_mock)

        asyncio.run(_go())
        assert sleep_mock.await_count == 1
        assert sleep_mock.await_args_list[0].args == (2.0,)


class TestNoRealSleeps:
    """Test suite MUST NOT spend real wall time on rate-limit delays."""

    def test_ten_second_wait_completes_instantly(
        self, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        b = TokenBucket(capacity=1.0, refill_rate=0.1, monotonic=fake_monotonic_clock)
        sleep_mock = _make_sleep_mock(advance_clock=fake_monotonic_clock)

        async def _go() -> None:
            await b.acquire(sleep_fn=sleep_mock)  # empty bucket
            # Second acquire: needs 1 token at 0.1/sec -> would wait 10s.
            await b.acquire(sleep_fn=sleep_mock)

        start = real_time.perf_counter()
        asyncio.run(_go())
        elapsed = real_time.perf_counter() - start
        # Real elapsed MUST be < 1s (asyncio scheduling overhead only).
        assert elapsed < 1.0
        # AsyncMock recorded the fake delay.
        assert sleep_mock.await_count == 1
        assert sleep_mock.await_args_list[0].args == (10.0,)
