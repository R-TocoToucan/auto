"""Pydantic allowlist models for the authenticated `/v1/orders/chance` response.

D-77 discipline: this module defines pydantic models covering ONLY the
allowlisted fields required for the snapshot builder. Extra keys raise
`ValidationError` (``extra="forbid"``) — a response with unexpected
top-level fields is a build-time-verification signal, not a
silently-ignored surface.

D-49 discipline: every decimal-bearing field uses ``StrictDecimal``
(the same annotated type used in :mod:`bithumb_bot.config.gate1_model`)
so a JSON float literal is rejected at parse time. Bithumb's docs
represent monetary values as quoted strings; if a real response ever
sneaks a float into a fee field, this rejection surfaces the drift
immediately rather than round-tripping through ``float``.

**M1 verification note:** the field set and nesting shape encoded
below track ``apidocs.bithumb.com`` per Finding 6 area. Any deviation
discovered during the live M1 verification (Open Verification Item #4
area) is recorded in ``VERIFICATION.md`` with a follow-up patch to
:mod:`.sanitize`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bithumb_bot.config.gate1_model import OptionalStrictDecimal, StrictDecimal


class MarketSide(BaseModel):
    """One side (bid or ask) of a `market.bid` / `market.ask` block."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    currency: str
    price_unit: OptionalStrictDecimal = None
    min_total: OptionalStrictDecimal = None


class MarketBlock(BaseModel):
    """The `market` sub-object of the `/v1/orders/chance` response."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str
    order_types: list[str] = Field(default_factory=list)
    bid: MarketSide | None = None
    ask: MarketSide | None = None
    max_total: OptionalStrictDecimal = None


class OrdersChanceResponse(BaseModel):
    """Allowlisted pydantic model of Bithumb's `/v1/orders/chance` response.

    Only the fields the snapshot builder needs are declared here. Extra
    top-level keys → ``ValidationError`` (D-77 allowlist, not blacklist).

    Every decimal-bearing field is `StrictDecimal` — a JSON float
    literal is rejected at parse time (D-49).
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    bid_fee: StrictDecimal
    ask_fee: StrictDecimal
    maker_bid_fee: OptionalStrictDecimal = None
    maker_ask_fee: OptionalStrictDecimal = None
    market: MarketBlock


__all__ = [
    "MarketBlock",
    "MarketSide",
    "OrdersChanceResponse",
]


# Re-export the allowlist key set for the sanitizer (single source of truth).
ORDERS_CHANCE_ALLOWLIST: frozenset[str] = frozenset(
    {"bid_fee", "ask_fee", "maker_bid_fee", "maker_ask_fee", "market"}
)


def _unused_marker(_: Any) -> None:  # pragma: no cover — silence unused import
    """Anchor the ``Any`` import; no runtime effect."""
    return None
