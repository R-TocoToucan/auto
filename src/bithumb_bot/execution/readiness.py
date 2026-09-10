"""Diagnostic: is a `SnapshotV1` compatible with :func:`execute_intent`?

Pure function. Does NOT verify artifact integrity — that is
:func:`bithumb_bot.bithumb_spec.snapshot.load_snapshot`'s job (hash
sidecar + schema validation). Given an already-loaded snapshot and the
strict/provisional fee-policy the engine would run under, this reports
which facts the engine consumes are missing or unresolved.

The rule enforced here MUST match the engine's fail-closed contract:
a snapshot flagged ``ready`` here must be one :func:`execute_intent`
would actually accept; a snapshot the engine would refuse MUST NOT be
flagged ``ready``. The provisional-fee opt-in is passed in explicitly
as a required keyword-only Boolean — the caller decides. Callers with
an :class:`~bithumb_bot.execution.config.ExecutionConfig` in hand pass
its ``allow_provisional_fee_model`` field; strict diagnostics (e.g.
``bt m1 verify-snapshot``) pass ``False`` explicitly. Requiring the
argument prevents this module from silently assuming a fee policy.

Missing / unresolved requirement keys returned to the caller (fixed
documented order — never set iteration order):

1. ``general_fee_rate``               — sell-side fee verification status
2. ``market_buy_fee_reservation``     — buy-side fee verification status
3. ``default_tick``                   — price tick from ``price_tick_rules``
4. ``default_step``                   — quantity step from ``quantity_step_rules``
5. ``fee_rates.bid``                  — bid (buy) fee rate presence
6. ``fee_rates.ask``                  — ask (sell) fee rate presence
7. ``krw_min_total_bid``              — buy minimum notional
8. ``krw_min_total_ask``              — sell minimum notional
9. ``market_buy_price_support``       — support for the market-buy `price` order
10. ``market_sell_market_support``    — support for the market-sell `market` order

Tri-state for the two order-support keys: they are looked up in
``snapshot.verification_status``. Present-and-``confirmed_read_only``
→ ready; anything else (or absent) → unresolved. Absence proves
nothing about the venue — it means "not established from this
snapshot," not "unsupported."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bithumb_bot.bithumb_spec.snapshot import SnapshotV1

# Keys are checked and reported in this exact order.
_CHECK_ORDER: tuple[str, ...] = (
    "general_fee_rate",
    "market_buy_fee_reservation",
    "default_tick",
    "default_step",
    "fee_rates.bid",
    "fee_rates.ask",
    "krw_min_total_bid",
    "krw_min_total_ask",
    "market_buy_price_support",
    "market_sell_market_support",
)


@dataclass(frozen=True)
class ReadinessReport:
    """Deterministic execution-readiness diagnostic for a snapshot.

    Attributes:
        execution_readiness:  ``"ready"`` iff every fact the engine
                              consumes is present and its verification
                              status is accepted under the supplied
                              fee-policy Boolean; ``"unresolved"``
                              otherwise. Artifact integrity is a
                              separate concern — see
                              :func:`~bithumb_bot.bithumb_spec.
                              snapshot.load_snapshot`.
        missing_requirements: The requirement keys that are missing or
                              unresolved, in the fixed documented order
                              (see module docstring). Empty tuple iff
                              ``execution_readiness == "ready"``.
    """

    execution_readiness: Literal["ready", "unresolved"]
    missing_requirements: tuple[str, ...]


def _fee_status_ready(
    status: str | None, *, allow_provisional: bool
) -> bool:
    """Return True iff the fee status is accepted by :func:`execute_intent`."""
    if status == "confirmed_read_only":
        return True
    if status == "provisional_documented" and allow_provisional:
        return True
    return False


def _order_support_ready(status: str | None) -> bool:
    """Tri-state: only ``confirmed_read_only`` is ``ready``. Missing → unresolved."""
    return status == "confirmed_read_only"


def check_execution_readiness(
    snapshot: SnapshotV1,
    *,
    allow_provisional_fee_model: bool,
) -> ReadinessReport:
    """Report whether the engine would accept ``snapshot`` under this policy.

    ``allow_provisional_fee_model`` is required (no default) so this
    module cannot silently assume a fee policy. Callers holding an
    :class:`~bithumb_bot.execution.config.ExecutionConfig` MUST pass
    ``config.allow_provisional_fee_model``; strict diagnostics MUST
    pass ``False`` explicitly.

    Args:
        snapshot:                    A loaded, sidecar-verified snapshot.
        allow_provisional_fee_model: Whether ``provisional_documented``
                                     fee-verification statuses count
                                     as ready. Required keyword-only.

    Returns:
        A :class:`ReadinessReport` whose ``missing_requirements`` are
        returned in the fixed documented order defined at module scope.
    """
    checks: dict[str, bool] = {}

    # 1. Sell-side fee verification.
    checks["general_fee_rate"] = _fee_status_ready(
        snapshot.verification_status.get("general_fee_rate"),
        allow_provisional=allow_provisional_fee_model,
    )
    # 2. Buy-side fee verification (market-buy fee reservation).
    checks["market_buy_fee_reservation"] = _fee_status_ready(
        snapshot.verification_status.get("market_buy_fee_reservation"),
        allow_provisional=allow_provisional_fee_model,
    )
    # 3-4. Price tick and quantity step.
    checks["default_tick"] = "default_tick" in snapshot.price_tick_rules
    checks["default_step"] = "default_step" in snapshot.quantity_step_rules
    # 5-6. Fee rates — pydantic makes ``bid`` / ``ask`` non-optional on
    # a valid snapshot, but check defensively so this report cannot
    # silently promise more than the SnapshotV1 shape actually holds.
    checks["fee_rates.bid"] = snapshot.fee_rates.bid is not None
    checks["fee_rates.ask"] = snapshot.fee_rates.ask is not None
    # 7-8. Minimum notionals.
    checks["krw_min_total_bid"] = snapshot.minimums.krw_min_total_bid is not None
    checks["krw_min_total_ask"] = snapshot.minimums.krw_min_total_ask is not None
    # 9-10. Order-type support (tri-state via verification_status).
    checks["market_buy_price_support"] = _order_support_ready(
        snapshot.verification_status.get("market_buy_price_support")
    )
    checks["market_sell_market_support"] = _order_support_ready(
        snapshot.verification_status.get("market_sell_market_support")
    )

    missing = tuple(key for key in _CHECK_ORDER if not checks[key])
    readiness: Literal["ready", "unresolved"] = "ready" if not missing else "unresolved"
    return ReadinessReport(
        execution_readiness=readiness,
        missing_requirements=missing,
    )


__all__ = ["ReadinessReport", "check_execution_readiness"]
