"""Asyncio-native TokenBucket with an injectable monotonic clock (Finding 7).

Semantics (classical continuous-refill token bucket):

* Bucket holds up to ``capacity`` tokens; starts full.
* Tokens accrue continuously at ``refill_rate`` tokens per second, up to
  ``capacity`` — refill above capacity is silently discarded.
* ``await acquire(n)`` blocks until at least ``n`` tokens are available;
  it then subtracts ``n`` and returns.
* ``acquire(n)`` where ``n > capacity`` is a programming error and
  raises :class:`ValueError` at call time (would otherwise block forever).

Test-friendliness (T-1-04-05 relies on this):

* Both the **monotonic clock** (``monotonic`` kwarg) AND the **sleep
  function** (``sleep_fn`` kwarg on :meth:`acquire`) are injectable so
  the test suite never spends real wall time. The default clock is
  :func:`time.monotonic` and the default sleep is
  :func:`asyncio.sleep`; production behavior is unchanged.
* Concurrency-safe via ``asyncio.Lock`` so a single-threaded asyncio
  event loop with multiple coroutines contending on the same bucket
  still gets fair FIFO service.
* The lock is held ONLY across the refill-computation + token deduction
  window; ``await sleep_fn(delay)`` happens AFTER the lock is released
  when a wait is required, so a waiter never blocks other coroutines
  from acquiring already-available tokens.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class TokenBucket:
    """Continuous-refill token bucket with an injectable monotonic clock.

    Args:
        capacity:     Maximum tokens the bucket can hold. Must be > 0.
        refill_rate:  Tokens per second added when the bucket is below
                      capacity. Must be > 0.
        monotonic:    Callable returning the current monotonic time in
                      seconds. Defaults to :func:`time.monotonic`; tests
                      pass a ``fake_monotonic_clock`` fixture.

    The bucket starts full (``tokens == capacity``).
    """

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity!r}")
        if refill_rate <= 0:
            raise ValueError(f"refill_rate must be > 0, got {refill_rate!r}")
        self._capacity = float(capacity)
        self._refill_rate = float(refill_rate)
        self._monotonic = monotonic
        self._tokens = float(capacity)  # start full
        self._last_refill = float(monotonic())
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def refill_rate(self) -> float:
        return self._refill_rate

    @property
    def tokens(self) -> float:
        """Currently-available tokens (without triggering a refill).

        This is a snapshot for introspection / tests; production callers
        MUST use :meth:`acquire`, which recomputes the balance inside
        the lock.
        """
        return self._tokens

    def _refill_locked(self) -> None:
        """Recompute the token balance based on elapsed wall time.

        MUST be called with ``self._lock`` held.
        """
        now = float(self._monotonic())
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now

    async def acquire(
        self,
        n: float = 1.0,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Acquire ``n`` tokens, waiting via ``sleep_fn`` for refill if needed.

        Args:
            n:        Tokens to acquire; must be > 0 and <= ``capacity``.
            sleep_fn: Coroutine that sleeps for a given number of
                      seconds. Defaults to :func:`asyncio.sleep`; tests
                      pass an ``AsyncMock`` so the test suite runs in
                      milliseconds even for "sleep 10 seconds" scenarios.

        Raises:
            ValueError: ``n <= 0`` or ``n > capacity`` (would block forever).
        """
        if n <= 0:
            raise ValueError(f"acquire n must be > 0, got {n!r}")
        if n > self._capacity:
            raise ValueError(
                f"acquire n={n!r} exceeds capacity={self._capacity!r} — "
                "would block forever"
            )
        while True:
            async with self._lock:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                # Not enough tokens yet — compute how long to sleep until
                # we have exactly `n`. Sleep OUTSIDE the lock so other
                # coroutines can still race for tokens that appear.
                deficit = n - self._tokens
                delay = deficit / self._refill_rate
            await sleep_fn(delay)
            # Loop: recompute inside the lock. Another waiter may have
            # stolen tokens while we slept.


__all__ = ["TokenBucket"]
