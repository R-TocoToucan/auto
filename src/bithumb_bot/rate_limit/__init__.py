"""`bithumb_bot.rate_limit` — per-channel client-side rate limiting.

Public surface is deliberately small: one class, :class:`TokenBucket`,
sufficient for the three per-channel instances the M1 spec adapter needs
(`public_rest`, `private_rest`, `public_ws` connection — see
:mod:`bithumb_bot.bithumb_spec.rate_limits`). Numeric capacity / refill
values are M1 build-time verification items (Open Verification Item #3
in the VERIFICATION.md bundle) — this module intentionally does NOT
hardcode Bithumb-specific values.
"""

from __future__ import annotations

from bithumb_bot.rate_limit.token_bucket import TokenBucket

__all__ = ["TokenBucket"]
