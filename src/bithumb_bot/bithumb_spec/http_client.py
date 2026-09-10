"""`httpx.AsyncClient` factory + retry loop honoring `Retry-After` (Finding 8).

D-40 retry semantics (frozen at Gate 1 for M1 read-only HTTP only —
MUST NOT be reused implicitly by the protective-exit path):

* Retryable HTTP status codes: ``408, 429, 500, 502, 503, 504``.
* Retryable network errors: ``ConnectTimeout``, ``ReadTimeout``,
  ``ConnectError``, ``NetworkError``.
* NON-retryable 4xx (return immediately): ``400, 401, 403, 404, 422``.
* When ``Retry-After`` is present AND parseable as an integer number
  of seconds → sleep that long. Otherwise: full-jitter exponential
  backoff ``min(cap_ms, initial_ms * 2**(attempt-1))`` with
  ``random.uniform(0, delay_ms) / 1000`` (per D-40 ``jitter=full``).
* Attempt limit ``max_attempts`` (D-40 frozen at 3). On exhaustion,
  return the last response (never ``raise RuntimeError('unreachable')``
  per Finding 8 — a caller that got repeated retryable 5xx needs to
  see it and decide).

Test-friendliness (D-79):

* ``create_client(transport=...)`` accepts an ``httpx.AsyncBaseTransport``
  so tests pass ``httpx.MockTransport`` — no real network I/O ever
  occurs in CI.
* ``sleep_fn`` on ``request_with_retry`` is injectable (default
  :func:`asyncio.sleep`) so tests use an ``AsyncMock`` and assert on
  the recorded sleep durations without spending wall time.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable

import httpx

from bithumb_bot.rate_limit.token_bucket import TokenBucket

# D-40 retry semantics.
_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_NON_RETRYABLE_4XX: frozenset[int] = frozenset({400, 401, 403, 404, 422})
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.NetworkError,
)


def create_client(
    *,
    base_url: str,
    connect_timeout_s: float,
    read_timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` with D-40 timeouts.

    Args:
        base_url:          The origin (e.g. ``https://api.bithumb.com``).
        connect_timeout_s: D-40 connect timeout in seconds
                           (Gate-1 frozen 5.0).
        read_timeout_s:    D-40 read timeout in seconds (Gate-1 frozen 15.0).
                           Also used for `write` and `pool` per Finding 8
                           — httpx requires an explicit value for each and
                           defaulting all four to the same read value is
                           the conservative choice for M1 (no long-poll
                           patterns here).
        transport:         Optional `httpx.AsyncBaseTransport`. Tests
                           pass `httpx.MockTransport` (D-79 — CI never
                           calls Bithumb).

    Returns:
        A fresh `httpx.AsyncClient`. Caller owns lifecycle
        (`async with create_client(...) as client:`).
    """
    timeout = httpx.Timeout(
        connect=connect_timeout_s,
        read=read_timeout_s,
        write=read_timeout_s,
        pool=read_timeout_s,
    )
    # trust_env=False: never silently inherit ambient proxy env vars
    # (HTTP_PROXY / HTTPS_PROXY / etc.). Exchange traffic must not route
    # through a shell-configured proxy the operator did not authorize.
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        trust_env=False,
    )


def _parse_retry_after(
    response: httpx.Response, *, max_seconds: float
) -> float | None:
    """Return the ``Retry-After`` header as bounded float seconds.

    Bithumb (per docs) uses integer seconds. HTTP allows a date form
    too — we accept ONLY the integer-seconds form; a date-form
    ``Retry-After`` returns ``None`` (caller falls back to backoff).

    The value is clamped to ``[0.0, max_seconds]``: a negative header
    cannot cause a failure, and an excessive header cannot cause
    indefinite delay. A malformed / missing header returns ``None``.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        parsed = float(int(value.strip()))
    except (ValueError, AttributeError):
        return None
    if parsed < 0:
        return 0.0
    if parsed > max_seconds:
        return max_seconds
    return parsed


def _backoff_delay_seconds(
    attempt: int,
    *,
    backoff_initial_ms: int,
    backoff_cap_ms: int,
    rng: random.Random,
) -> float:
    """Full-jitter exponential backoff (D-40 ``jitter=full``).

    ``delay_ms = min(cap_ms, initial_ms * 2**(attempt-1))``
    ``return uniform(0, delay_ms) / 1000``

    Args:
        attempt:            1-based attempt index (1 → first retry).
        backoff_initial_ms: D-40 500.
        backoff_cap_ms:     D-40 5000.
        rng:                Injectable random source (tests pass a
                            seeded ``Random`` for determinism).
    """
    exp = min(backoff_cap_ms, backoff_initial_ms * (2 ** (attempt - 1)))
    return rng.uniform(0, exp) / 1000.0


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int,
    backoff_initial_ms: int,
    backoff_cap_ms: int,
    bucket: TokenBucket,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Retryable HTTP request per D-40.

    Sequence for each attempt:

    1. ``await bucket.acquire()`` before the request (rate limit).
    2. Try ``client.request(method, url, **kwargs)``.
    3. On success (status not in the retryable set) → return.
    4. On retryable 5xx / 408 / 429 → sleep (``Retry-After`` if
       parseable, else full-jitter backoff) and retry.
    5. On non-retryable 4xx (400/401/403/404/422) → return immediately.
    6. On retryable exception (Timeout/Connect/Network) → sleep and
       retry with full-jitter backoff.
    7. On max-attempt exhaustion → return the last response (Finding 8:
       never eat it as ``RuntimeError('unreachable')``).

    Args:
        client:             The `httpx.AsyncClient` to use.
        method / url:       HTTP method + path.
        max_attempts:       D-40 frozen 3.
        backoff_initial_ms: D-40 500.
        backoff_cap_ms:     D-40 5000.
        bucket:             The per-channel rate limiter to acquire on
                            every attempt (T-1-04-05).
        sleep_fn:           Injectable sleep (tests pass AsyncMock).
        rng:                Injectable RNG (tests pass a seeded Random).
        **kwargs:           Passed through to `client.request`.

    Returns:
        The final `httpx.Response` (success OR final retryable failure
        after max_attempts).

    Raises:
        The last retryable exception if ``max_attempts`` was exhausted
        WITHOUT a response ever being received (all attempts raised).
    """
    rng = rng or random.Random()
    last_response: httpx.Response | None = None
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        await bucket.acquire(sleep_fn=sleep_fn)
        try:
            response = await client.request(method, url, **kwargs)
        except _RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= max_attempts:
                # Exhausted. Re-raise so the caller sees the network error.
                raise
            delay = _backoff_delay_seconds(
                attempt,
                backoff_initial_ms=backoff_initial_ms,
                backoff_cap_ms=backoff_cap_ms,
                rng=rng,
            )
            await sleep_fn(delay)
            continue

        last_response = response
        status = response.status_code
        if status in _NON_RETRYABLE_4XX or status not in _RETRYABLE_STATUS:
            return response
        # Retryable status.
        if attempt >= max_attempts:
            return response
        # Bound Retry-After to the same cap window as the backoff loop
        # so a hostile / malformed header cannot cause indefinite delay.
        retry_after = _parse_retry_after(
            response, max_seconds=backoff_cap_ms / 1000.0
        )
        if retry_after is not None:
            await sleep_fn(retry_after)
        else:
            delay = _backoff_delay_seconds(
                attempt,
                backoff_initial_ms=backoff_initial_ms,
                backoff_cap_ms=backoff_cap_ms,
                rng=rng,
            )
            await sleep_fn(delay)
    # Unreachable in practice — the loop returns or raises above.
    if last_response is not None:
        return last_response
    assert last_exc is not None
    raise last_exc  # pragma: no cover


__all__ = ["create_client", "request_with_retry"]
