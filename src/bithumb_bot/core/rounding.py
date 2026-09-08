"""`bithumb_bot.core.rounding` — direction-aware quantize helpers.

The Bithumb spot venue publishes discrete `price_tick`, `qty_step`, and
`min_krw_amount_unit` values per market. Order-placement and P&L math
must snap to these grids in the **adverse** direction so we never
under-quote costs or over-quote available capital.

D-49 rules the base decision: everything is ``Decimal`` and never
constructed from a ``float``. D-50 and D-51 pick the direction for
price ticks: **round up for buy fills**, **round down for sell fills**
— either choice moves the quoted price *away* from the counterparty's
better side.

Four helpers
------------

* ``quantize_krw_amount_down(amount, unit)`` — floor the KRW notional
  to a whole multiple of ``unit`` (never over-quote available cash).
* ``quantize_volume_down(volume, step)`` — floor the base-asset
  quantity to a whole multiple of ``step`` (never claim more than the
  venue will accept).
* ``quantize_price_tick_up(price, tick)`` — ceil the price to a whole
  multiple of ``tick`` (adverse for BUY fills, D-50).
* ``quantize_price_tick_down(price, tick)`` — floor the price to a
  whole multiple of ``tick`` (adverse for SELL fills, D-51).

All four
--------
* Take and return only ``Decimal`` — no coercion to ``Money`` / ``Qty``
  (that wrapping is the caller's responsibility so these primitives
  stay reusable across sizing / accounting / broker-adapter contexts).
* Reject non-positive ``unit`` / ``step`` / ``tick`` with
  ``ValueError``.
* Are idempotent: applying the same helper twice yields the same
  result.

These helpers ship WITHOUT hardcoded Bithumb `min_krw_amount_unit` /
`qty_step` / `price_tick` values — those come from the M1 spec
snapshot (plan 01-04). Wiring the snapshot values into these
primitives happens in Phase 2 (M2).
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal


def _require_positive(unit: Decimal, name: str) -> None:
    if unit <= 0:
        raise ValueError(
            f"{name} must be a positive Decimal, got {unit!r}. Non-positive "
            "quantize units cannot express a grid."
        )


def _floor_to_multiple(x: Decimal, unit: Decimal) -> Decimal:
    """Return the largest multiple of ``unit`` that is <= ``x``.

    Uses integer arithmetic on the ratio so the answer is exact and
    idempotent for any positive `Decimal` `unit`, including sub-unit
    ticks like ``Decimal("0.00000001")``.
    """
    # (x // unit) * unit — Decimal floor division yields the exact
    # integer number of ticks that fit, then multiplication restores
    # the grid representation.
    ticks = (x / unit).to_integral_value(rounding=ROUND_DOWN)
    return ticks * unit


def _ceil_to_multiple(x: Decimal, unit: Decimal) -> Decimal:
    """Return the smallest multiple of ``unit`` that is >= ``x``."""
    ticks = (x / unit).to_integral_value(rounding=ROUND_UP)
    return ticks * unit


def quantize_krw_amount_down(amount: Decimal, unit: Decimal) -> Decimal:
    """Floor `amount` (KRW notional) to a whole multiple of `unit` (D-49)."""
    _require_positive(unit, "unit")
    return _floor_to_multiple(amount, unit)


def quantize_volume_down(volume: Decimal, step: Decimal) -> Decimal:
    """Floor `volume` (base-asset quantity) to a whole multiple of `step` (D-49)."""
    _require_positive(step, "step")
    return _floor_to_multiple(volume, step)


def quantize_price_tick_up(price: Decimal, tick: Decimal) -> Decimal:
    """Ceil `price` to a whole multiple of `tick` (adverse for BUY fills, D-50)."""
    _require_positive(tick, "tick")
    return _ceil_to_multiple(price, tick)


def quantize_price_tick_down(price: Decimal, tick: Decimal) -> Decimal:
    """Floor `price` to a whole multiple of `tick` (adverse for SELL fills, D-51)."""
    _require_positive(tick, "tick")
    return _floor_to_multiple(price, tick)


__all__ = [
    "quantize_krw_amount_down",
    "quantize_price_tick_down",
    "quantize_price_tick_up",
    "quantize_volume_down",
]
