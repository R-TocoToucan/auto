"""Allowlist-only sanitizer for the `/v1/orders/chance` raw response (D-77).

The D-77 rule is **allowlist**, NOT blacklist. Given a raw
authenticated response `dict`, the sanitizer constructs its output by
picking each field named in :data:`ORDERS_CHANCE_ALLOWLIST` — it
**never** ``deepcopy``s the raw and then deletes forbidden keys. That
inversion is what makes T-1-04-02 hard to regress: a new forbidden
top-level field added by Bithumb has no path into the sanitized fixture
unless the allowlist is explicitly extended.

Forbidden fields explicitly asserted absent by the test suite:

* ``access_key``, ``secret_key``
* ``Authorization``, ``Set-Cookie``
* ``nonce``, ``signature``
* ``account_id``, ``balance``, ``avg_buy_price``, ``locked_balance``

Nested forbidden keys are absent by construction (the sanitizer never
descends into an unlisted top-level key, so a nested
``{"headers": {"Authorization": ...}}`` disappears with the ``headers``
outer dict).
"""

from __future__ import annotations

from typing import Any, Mapping

from bithumb_bot.bithumb_spec.schemas import ORDERS_CHANCE_ALLOWLIST


def sanitize_orders_chance(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return an allowlist-only sanitized copy of ``raw``.

    Args:
        raw: The raw JSON-parsed authenticated response body.

    Returns:
        A new ``dict`` containing ONLY keys in
        :data:`ORDERS_CHANCE_ALLOWLIST`. Missing allowlist keys are
        simply absent from the output — the pydantic model
        (``OrdersChanceResponse``) is responsible for enforcing which
        keys are required.
    """
    out: dict[str, Any] = {}
    for key in ORDERS_CHANCE_ALLOWLIST:
        if key in raw:
            value = raw[key]
            if isinstance(value, Mapping):
                # For nested allowlisted keys we recurse ONLY on the
                # published nested allowlist (`market.*`). Any nested
                # key not in the recursive allowlist is dropped by the
                # same construction principle.
                if key == "market":
                    out[key] = _sanitize_market(value)
                    continue
            out[key] = value
    return out


_MARKET_ALLOWLIST: frozenset[str] = frozenset(
    {"name", "order_types", "bid", "ask", "max_total"}
)
_MARKET_SIDE_ALLOWLIST: frozenset[str] = frozenset(
    {"currency", "price_unit", "min_total"}
)


def _sanitize_market(market: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _MARKET_ALLOWLIST:
        if key not in market:
            continue
        value = market[key]
        if key in ("bid", "ask") and isinstance(value, Mapping):
            out[key] = _sanitize_market_side(value)
        else:
            out[key] = value
    return out


def _sanitize_market_side(side: Mapping[str, Any]) -> dict[str, Any]:
    return {k: side[k] for k in _MARKET_SIDE_ALLOWLIST if k in side}


__all__ = ["sanitize_orders_chance"]
