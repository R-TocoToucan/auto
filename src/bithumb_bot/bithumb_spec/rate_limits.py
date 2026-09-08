"""Three per-channel `TokenBucket` instances for the M1 spec adapter.

Bithumb (per Finding 7 area, verified only as documentation this pass)
imposes separate rate limits on:

* Public REST endpoints (`public_rest`).
* Private / authenticated REST endpoints (`private_rest`).
* Public WebSocket connections (`public_ws` — per WS connection).

**Numeric values below are TEST-ONLY sentinels** — the real per-channel
`capacity` and `refill_rate` are Open Verification Item #3 in the
`VERIFICATION.md` bundle and MUST be frozen there BEFORE any M1
execution against a live endpoint. This file is the single-point-of-
change: consumers pull the module-level bucket instances rather than
constructing their own, so replacing the sentinel with real values
later is a one-line diff.

**These sentinel values are separate from D-40** (M1 read-only HTTP
timeout / retry values, Gate-1 frozen: connect 5s, read 15s,
max_attempts 3, backoff 500ms/5000ms, full jitter). D-40 governs the
retry loop; per-channel token buckets govern the request cadence.

TODO(M1-verify): swap `capacity` / `refill_rate` for the observed
values recorded in the M1 `VERIFICATION.md` bundle. See Open
Verification Item #3.
"""

from __future__ import annotations

from bithumb_bot.rate_limit.token_bucket import TokenBucket

# Sentinel values — MUST be replaced before live M1 execution.
# Chosen deliberately low so any accidental live use is obviously wrong.
_SENTINEL_CAPACITY = 1.0
_SENTINEL_REFILL_RATE = 0.5

# TEST-ONLY sentinel — Bithumb public REST rate limit is Open
# Verification Item #3 (see VERIFICATION.md).
public_rest: TokenBucket = TokenBucket(
    capacity=_SENTINEL_CAPACITY, refill_rate=_SENTINEL_REFILL_RATE
)

# TEST-ONLY sentinel — Bithumb private/authenticated REST rate limit
# is Open Verification Item #3.
private_rest: TokenBucket = TokenBucket(
    capacity=_SENTINEL_CAPACITY, refill_rate=_SENTINEL_REFILL_RATE
)

# TEST-ONLY sentinel — Bithumb public WebSocket per-connection rate
# limit is Open Verification Item #3.
public_ws: TokenBucket = TokenBucket(
    capacity=_SENTINEL_CAPACITY, refill_rate=_SENTINEL_REFILL_RATE
)


__all__ = ["private_rest", "public_rest", "public_ws"]
