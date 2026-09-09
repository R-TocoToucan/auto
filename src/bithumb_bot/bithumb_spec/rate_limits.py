"""Three per-channel `TokenBucket` instances for the M1 spec adapter.

Bithumb (per Finding 7 area) exposes separate rate limits on:

* Public REST endpoints (`public_rest`).
* Private / authenticated REST endpoints (`private_rest`).
* Public WebSocket connection attempts (`public_ws`).

Operational limits (conservative — NOT the exchange ceiling)
------------------------------------------------------------
The values below are **project operational limits** deliberately set
FAR below Bithumb's documented ceilings, so a mis-configured retry
loop, a runaway coroutine, or a partial-outage recovery cannot burn
through the exchange's real quota. They are *not* claims about how
much throughput Bithumb allows.

* `public_rest`  — capacity=5,  refill_rate=5  req/s   (project operational cap)
* `private_rest` — capacity=2,  refill_rate=2  req/s   (project operational cap)
* `public_ws`    — capacity=1,  refill_rate=1  conn/s  (project operational cap)

Documented Bithumb ceilings observed 2026-09-09 (source below)
--------------------------------------------------------------
Recorded here for provenance ONLY; the operational caps above are
what the client actually uses. Do NOT wire these ceilings into the
`TokenBucket` arguments.

* Public REST:  150 req/s per IP per category (categories: Candles,
  Order Book, Current Price, Trades, Other).
* Private REST: 140 req/s per IP per category (categories: Order
  Request, Order Cancellation, Other — plus a stricter 20 req/s
  cap on bulk-order endpoints which this project does not use).
* Public WebSocket connection attempts: 10 conn/s per IP; sustained
  excess ⇒ 10-minute IP ban.

Source: https://apidocs.bithumb.com/docs/api-요청-수-제한-안내
        (page's last-updated header: 2026-08-26; fetched 2026-09-09).
Cross-checked against https://apidocs.bithumb.com/reference/기본-정보
(WebSocket 10 conn/s/IP + 429 + 10-min ban).

Explicit caveats — NOT to be silently relaxed
---------------------------------------------
1. **Bithumb may lower published limits without notice.** The values
   above are a snapshot on 2026-09-09; treat them as best-effort
   documentation, not a contract. The operational caps are set with
   enough headroom that a plausible near-term reduction does not
   invalidate them.
2. **`public_ws` governs CONNECTION ATTEMPTS only**, not the rate of
   incoming WebSocket messages or subscription payloads. Message-rate
   handling (backpressure, dedup after reconnect) is separate.
3. **Classification of `GET /v1/orders/chance` under Private → "Other"
   (기타) is an inference**, not an endpoint-specific statement in
   the docs. `/v1/orders/chance` is a read-only account-info query,
   which matches the "Other" bucket's description; a live-observed
   429 pattern would confirm — currently unverified.
4. **REST 429 response body / header behavior remains unresolved.**
   The Bithumb REST rate-limit page states only that "usage of that
   item's API is temporarily restricted" without documenting an HTTP
   status code, `Retry-After` header, or `X-RateLimit-*` semantics.
   Do NOT invent a response shape. The D-40 retry loop's fixed
   exponential backoff (500 ms → 5 s, 3 attempts, full jitter) is
   the safe stand-in until M6B live observation freezes the format.
5. **Per-category quotas are collapsed into one bucket per channel**
   (see "분류 항목별" in the source). This is deliberately more
   conservative than the spec allows — a project-wide simplification
   that avoids splitting buckets by category (which would grow the
   TokenBucket surface without a demonstrated need).

D-40 vs. per-channel rate limits — separate concerns
----------------------------------------------------
D-40 (Gate-1 frozen) governs the M1 read-only HTTP retry loop:
connect 5s, read 15s, max_attempts 3, backoff 500ms/5000ms with full
jitter. Per-channel token buckets govern REQUEST CADENCE. Both apply
to every M1 REST call; neither replaces the other.

Verification-item status
------------------------
Open Verification Item #3 (per-channel rate-limit numeric values) is
resolved for the READ path at the conservative-operational level
recorded here. Live-observed 429 semantics remain a follow-up for
M6B.
"""

from __future__ import annotations

from bithumb_bot.rate_limit.token_bucket import TokenBucket


# Conservative project operational limits — far below the documented
# Bithumb ceilings recorded in the module docstring. Chosen to leave
# room for an unannounced ceiling reduction and to prevent a retry
# loop from burning through the real quota.
_PUBLIC_REST_CAPACITY = 5.0
_PUBLIC_REST_REFILL_RATE = 5.0
_PRIVATE_REST_CAPACITY = 2.0
_PRIVATE_REST_REFILL_RATE = 2.0
_PUBLIC_WS_CAPACITY = 1.0
_PUBLIC_WS_REFILL_RATE = 1.0

public_rest: TokenBucket = TokenBucket(
    capacity=_PUBLIC_REST_CAPACITY, refill_rate=_PUBLIC_REST_REFILL_RATE
)

private_rest: TokenBucket = TokenBucket(
    capacity=_PRIVATE_REST_CAPACITY, refill_rate=_PRIVATE_REST_REFILL_RATE
)

public_ws: TokenBucket = TokenBucket(
    capacity=_PUBLIC_WS_CAPACITY, refill_rate=_PUBLIC_WS_REFILL_RATE
)


__all__ = ["private_rest", "public_rest", "public_ws"]
