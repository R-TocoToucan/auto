"""Tests for :mod:`bithumb_bot.bithumb_spec.http_client` (D-40 semantics)."""

from __future__ import annotations

import asyncio
import random
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from bithumb_bot.bithumb_spec.http_client import create_client, request_with_retry
from bithumb_bot.rate_limit.token_bucket import TokenBucket
from tests.conftest import FakeMonotonicClock


def _make_bucket(fake_monotonic_clock: FakeMonotonicClock) -> TokenBucket:
    return TokenBucket(capacity=100.0, refill_rate=100.0, monotonic=fake_monotonic_clock)


def _sleep_mock() -> AsyncMock:
    return AsyncMock(return_value=None)


class TestCreateClient:
    def test_returns_asyncclient_with_timeout(self) -> None:
        c = create_client(base_url="https://x", connect_timeout_s=1.0, read_timeout_s=2.0)
        assert isinstance(c, httpx.AsyncClient)
        # httpx.Timeout exposes per-op values.
        assert c.timeout.connect == 1.0
        assert c.timeout.read == 2.0
        assert c.timeout.write == 2.0
        assert c.timeout.pool == 2.0

    def test_accepts_mock_transport(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        c = create_client(
            base_url="https://x",
            connect_timeout_s=1.0,
            read_timeout_s=1.0,
            transport=transport,
        )
        assert isinstance(c, httpx.AsyncClient)


class TestRetryAfterHonored:
    def test_429_retry_after_2_seconds(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        """429 Retry-After: 2 twice then 200 → two retries with sleep(2)."""
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            if call_count[0] < 3:
                return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "rate"})
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        client = create_client(
            base_url="https://x", connect_timeout_s=1.0, read_timeout_s=1.0, transport=transport
        )
        bucket = _make_bucket(fake_monotonic_clock)
        sleep = _sleep_mock()

        async def _go() -> Any:
            async with client as c:
                return await request_with_retry(
                    c,
                    "GET",
                    "/x",
                    max_attempts=3,
                    backoff_initial_ms=500,
                    backoff_cap_ms=5000,
                    bucket=bucket,
                    sleep_fn=sleep,
                )

        response = asyncio.run(_go())
        assert response.status_code == 200
        # Two Retry-After sleeps of 2.0 seconds; bucket.acquire uses the
        # same sleep mock but never triggers a wait because bucket is
        # over-capacity.
        # Filter to durations >= 1.0 (bucket wouldn't sleep long).
        retry_sleeps = [call.args[0] for call in sleep.await_args_list if call.args and call.args[0] >= 1.0]
        assert retry_sleeps == [2.0, 2.0]


class TestNonRetryable4xx:
    def test_403_returned_immediately(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            return httpx.Response(403, json={"error": "forbidden"})

        transport = httpx.MockTransport(handler)
        client = create_client(
            base_url="https://x", connect_timeout_s=1.0, read_timeout_s=1.0, transport=transport
        )
        bucket = _make_bucket(fake_monotonic_clock)
        sleep = _sleep_mock()

        async def _go() -> Any:
            async with client as c:
                return await request_with_retry(
                    c,
                    "GET",
                    "/x",
                    max_attempts=3,
                    backoff_initial_ms=500,
                    backoff_cap_ms=5000,
                    bucket=bucket,
                    sleep_fn=sleep,
                )

        response = asyncio.run(_go())
        assert response.status_code == 403
        assert call_count[0] == 1
        # No retry sleeps.
        retry_sleeps = [call.args[0] for call in sleep.await_args_list if call.args and call.args[0] >= 0.001]
        assert retry_sleeps == []

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_all_non_retryable_4xx_return_immediately(
        self, status: int, fake_monotonic_clock: FakeMonotonicClock
    ) -> None:
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            return httpx.Response(status)

        transport = httpx.MockTransport(handler)
        client = create_client(
            base_url="https://x", connect_timeout_s=1.0, read_timeout_s=1.0, transport=transport
        )
        bucket = _make_bucket(fake_monotonic_clock)
        sleep = _sleep_mock()

        async def _go() -> Any:
            async with client as c:
                return await request_with_retry(
                    c,
                    "GET",
                    "/x",
                    max_attempts=3,
                    backoff_initial_ms=500,
                    backoff_cap_ms=5000,
                    bucket=bucket,
                    sleep_fn=sleep,
                )

        response = asyncio.run(_go())
        assert response.status_code == status
        assert call_count[0] == 1


class TestRetryable5xx:
    def test_500_exhausted_returns_last_response(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        """500 three times with max_attempts=3 → return 500 after 3 attempts;
        sleep called twice with jitter within backoff bounds."""
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            return httpx.Response(500)

        transport = httpx.MockTransport(handler)
        client = create_client(
            base_url="https://x", connect_timeout_s=1.0, read_timeout_s=1.0, transport=transport
        )
        bucket = _make_bucket(fake_monotonic_clock)
        sleep = _sleep_mock()
        rng = random.Random(42)

        async def _go() -> Any:
            async with client as c:
                return await request_with_retry(
                    c,
                    "GET",
                    "/x",
                    max_attempts=3,
                    backoff_initial_ms=500,
                    backoff_cap_ms=5000,
                    bucket=bucket,
                    sleep_fn=sleep,
                    rng=rng,
                )

        response = asyncio.run(_go())
        assert response.status_code == 500
        assert call_count[0] == 3
        # Retry sleeps: 2 backoff calls between the 3 attempts.
        # attempt 1 -> delay ∈ [0, 500ms]
        # attempt 2 -> delay ∈ [0, 1000ms]
        retry_sleeps = [call.args[0] for call in sleep.await_args_list if call.args and 0 < call.args[0] < 100.0]
        assert len(retry_sleeps) == 2
        assert 0 <= retry_sleeps[0] <= 0.5
        assert 0 <= retry_sleeps[1] <= 1.0


class TestRetryableExceptions:
    def test_connect_timeout_twice_then_200(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            if call_count[0] < 3:
                raise httpx.ConnectTimeout("simulated")
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = create_client(
            base_url="https://x", connect_timeout_s=1.0, read_timeout_s=1.0, transport=transport
        )
        bucket = _make_bucket(fake_monotonic_clock)
        sleep = _sleep_mock()
        rng = random.Random(1)

        async def _go() -> Any:
            async with client as c:
                return await request_with_retry(
                    c,
                    "GET",
                    "/x",
                    max_attempts=3,
                    backoff_initial_ms=500,
                    backoff_cap_ms=5000,
                    bucket=bucket,
                    sleep_fn=sleep,
                    rng=rng,
                )

        response = asyncio.run(_go())
        assert response.status_code == 200
        assert call_count[0] == 3

    def test_all_attempts_raise_re_raises(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("always")

        transport = httpx.MockTransport(handler)
        client = create_client(
            base_url="https://x", connect_timeout_s=1.0, read_timeout_s=1.0, transport=transport
        )
        bucket = _make_bucket(fake_monotonic_clock)
        sleep = _sleep_mock()

        async def _go() -> Any:
            async with client as c:
                return await request_with_retry(
                    c,
                    "GET",
                    "/x",
                    max_attempts=3,
                    backoff_initial_ms=500,
                    backoff_cap_ms=5000,
                    bucket=bucket,
                    sleep_fn=sleep,
                )

        with pytest.raises(httpx.ConnectError):
            asyncio.run(_go())


class TestBucketAcquireBeforeEveryAttempt:
    def test_acquire_called_per_attempt(self, fake_monotonic_clock: FakeMonotonicClock) -> None:
        """T-1-04-05: bucket.acquire MUST run before every attempt."""
        acquire_count = [0]

        class _CountingBucket:
            async def acquire(self, n: float = 1.0, *, sleep_fn: Any = None) -> None:
                acquire_count[0] += 1

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        transport = httpx.MockTransport(handler)
        client = create_client(
            base_url="https://x", connect_timeout_s=1.0, read_timeout_s=1.0, transport=transport
        )
        sleep = _sleep_mock()

        async def _go() -> Any:
            async with client as c:
                return await request_with_retry(
                    c,
                    "GET",
                    "/x",
                    max_attempts=3,
                    backoff_initial_ms=500,
                    backoff_cap_ms=5000,
                    bucket=_CountingBucket(),  # type: ignore[arg-type]
                    sleep_fn=sleep,
                )

        asyncio.run(_go())
        assert acquire_count[0] == 3
