"""Official Bithumb KRW spot price-tick schedule (documented, 2026-09-10).

Sources:

* https://apidocs.bithumb.com/reference/주문-요청
* https://github.com/bithumb-official/bithumb-ai-trade-kit/blob/main/skills/bithumb-trade/references/order-commands.md

The schedule is a piecewise-constant function of the order price. The
returned tick is the KRW price increment the venue requires for an
order at that price. All arithmetic is Decimal-native — no floats — so
the tick returned for any Decimal input is exact.

The schedule is a documented Bithumb rule. It is separate from live
order-volume precision (currently unresolved) and from live venue
acceptance (unresolved until M6B). Callers that need those must not
route through this module.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Final

TICK_SCHEDULE_SOURCE_URL: Final[str] = (
    "https://apidocs.bithumb.com/reference/주문-요청"
)
TICK_SCHEDULE_SECONDARY_URL: Final[str] = (
    "https://github.com/bithumb-official/bithumb-ai-trade-kit/blob/main/"
    "skills/bithumb-trade/references/order-commands.md"
)
TICK_SCHEDULE_ACCESS_DATE_UTC: Final[str] = "2026-09-10"
TICK_SCHEDULE_VERSION: Final[str] = "krw-spot-2026-09-10"

#: Piecewise-constant KRW price-tick schedule. Each ``(upper_exclusive,
#: tick_size)`` pair states: ``price < upper_exclusive`` → this tick.
#: The last band (``price >= 1_000_000``) is handled outside the tuple.
KRW_TICK_BANDS: Final[tuple[tuple[Decimal, Decimal], ...]] = (
    (Decimal("1"), Decimal("0.0001")),
    (Decimal("10"), Decimal("0.001")),
    (Decimal("100"), Decimal("0.01")),
    (Decimal("5000"), Decimal("1")),
    (Decimal("10000"), Decimal("5")),
    (Decimal("50000"), Decimal("10")),
    (Decimal("100000"), Decimal("50")),
    (Decimal("500000"), Decimal("100")),
    (Decimal("1000000"), Decimal("500")),
)

#: Tick applied to prices ``>= 1_000_000 KRW``.
KRW_TICK_LARGE: Final[Decimal] = Decimal("1000")


def resolve_krw_tick(price: Decimal) -> Decimal:
    """Return the Bithumb KRW price tick for ``price``.

    Args:
        price: Positive :class:`Decimal` price at which the order sits.
            Must be ``> 0`` — a non-positive price cannot pick a band.

    Returns:
        The tick :class:`Decimal` for the corresponding band. Exact.

    Raises:
        TypeError:  ``price`` is not a :class:`Decimal` (D-49 discipline).
        ValueError: ``price`` is not strictly positive.
    """
    if type(price) is not Decimal:  # noqa: E721
        raise TypeError(
            f"resolve_krw_tick requires Decimal, got "
            f"{type(price).__name__!r} — the schedule cannot be selected "
            "from a float or int without violating D-49."
        )
    if price <= 0:
        raise ValueError(
            f"resolve_krw_tick requires price > 0, got {price!r}"
        )
    for upper_exclusive, tick in KRW_TICK_BANDS:
        if price < upper_exclusive:
            return tick
    return KRW_TICK_LARGE


def _schedule_content_hash() -> str:
    """Deterministic SHA-256 over the schedule content (for provenance)."""
    payload = {
        "version": TICK_SCHEDULE_VERSION,
        "bands": [
            [str(upper), str(tick)] for upper, tick in KRW_TICK_BANDS
        ],
        "large_tick": str(KRW_TICK_LARGE),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()


TICK_SCHEDULE_SHA256: Final[str] = _schedule_content_hash()


def tick_schedule_provenance() -> dict[str, str]:
    """Return the provenance record embedded into snapshots.

    Callers that store this MUST treat it as append-only research
    provenance — not as a live-order authorization surface.
    """
    return {
        "source_url": TICK_SCHEDULE_SOURCE_URL,
        "secondary_url": TICK_SCHEDULE_SECONDARY_URL,
        "access_date_utc": TICK_SCHEDULE_ACCESS_DATE_UTC,
        "schedule_version": TICK_SCHEDULE_VERSION,
        "schedule_sha256": TICK_SCHEDULE_SHA256,
    }


__all__ = [
    "KRW_TICK_BANDS",
    "KRW_TICK_LARGE",
    "TICK_SCHEDULE_ACCESS_DATE_UTC",
    "TICK_SCHEDULE_SECONDARY_URL",
    "TICK_SCHEDULE_SHA256",
    "TICK_SCHEDULE_SOURCE_URL",
    "TICK_SCHEDULE_VERSION",
    "resolve_krw_tick",
    "tick_schedule_provenance",
]
