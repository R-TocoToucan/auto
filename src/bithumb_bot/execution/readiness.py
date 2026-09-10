"""Diagnostic: is a `SnapshotV1` compatible with :func:`execute_intent`?

Batch 1B splits the single readiness surface into two:

* :attr:`ReadinessReport.research_simulation_readiness` — the strategy
  can be exercised under the conservative research configuration
  (documented order-type support, documented price-tick schedule,
  operator-supplied research quantum, explicit provisional-fee
  opt-in). ``confirmed_documented`` order-type support satisfies this
  surface but is NOT a live-authorization guarantee.
* :attr:`ReadinessReport.live_execution_readiness` — the snapshot is
  ready to drive a real trade against Bithumb. This surface is strict:
  fees must be ``confirmed_read_only``, order-type support must be
  ``confirmed_read_only`` (live-observed accepts, not just doc), the
  snapshot must expose a verified quantity step, and
  ``live_order_acceptance`` must be ``confirmed_read_only``. For a
  KRW-BTC snapshot in the current M1 surface this stays ``unresolved``
  until M6B confirms accepted order-volume precision, market-buy fee
  reservation, credential permission, and live order acceptance.

The provisional-fee opt-in is required keyword-only so this module
cannot silently assume a fee policy; callers holding an
:class:`~bithumb_bot.execution.config.ExecutionConfig` pass
``config.allow_provisional_fee_model``. The research quantum
(``simulation_quantity_quantum``) is also a required keyword — passing
``None`` deliberately makes research readiness unresolved, because a
simulator cannot floor quantities without one.

Missing-requirement keys (fixed documented order — never dict/set
iteration):

Research surface:

1. ``general_fee_rate``                      (fee status)
2. ``market_buy_fee_reservation``            (fee status)
3. ``price_tick_source``                     (schedule provenance OR default_tick)
4. ``simulation_quantity_quantum``           (research assumption)
5. ``fee_rates.bid``
6. ``fee_rates.ask``
7. ``krw_min_total_bid``
8. ``krw_min_total_ask``
9. ``market_buy_price_support``              (documented OR live-confirmed)
10. ``market_sell_market_support``           (documented OR live-confirmed)

Live surface (strictly ``confirmed_read_only`` everywhere):

1. ``general_fee_rate``
2. ``market_buy_fee_reservation``
3. ``default_tick``                          (live schedule not accepted from docs alone here)
4. ``default_step``                          (venue-confirmed live step)
5. ``fee_rates.bid``
6. ``fee_rates.ask``
7. ``krw_min_total_bid``
8. ``krw_min_total_ask``
9. ``market_buy_price_support``
10. ``market_sell_market_support``
11. ``live_order_acceptance``
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from bithumb_bot.bithumb_spec.snapshot import SnapshotV1


_RESEARCH_CHECK_ORDER: tuple[str, ...] = (
    "general_fee_rate",
    "market_buy_fee_reservation",
    "price_tick_source",
    "simulation_quantity_quantum",
    "fee_rates.bid",
    "fee_rates.ask",
    "krw_min_total_bid",
    "krw_min_total_ask",
    "market_buy_price_support",
    "market_sell_market_support",
)

_LIVE_CHECK_ORDER: tuple[str, ...] = (
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
    "live_order_acceptance",
)


@dataclass(frozen=True)
class ReadinessReport:
    """Two-surface readiness diagnostic for a loaded snapshot.

    Attributes:
        research_simulation_readiness:
            ``"ready"`` iff the snapshot + supplied research
            configuration can drive the conservative simulator.
            ``confirmed_documented`` order-type support satisfies
            this surface.
        live_execution_readiness:
            ``"ready"`` iff the snapshot alone can drive a real trade.
            Requires strict ``confirmed_read_only`` on every fee /
            order-type / live-acceptance status AND a snapshot-recorded
            venue quantity step — none of which the M1 read path can
            confirm without live venue interaction (deferred to M6B).
        research_missing_requirements:
            Keys blocking the research surface, in the fixed documented
            order at module scope. Empty when ready.
        live_missing_requirements:
            Keys blocking the live surface, in the fixed documented
            order at module scope. Empty when ready.
    """

    research_simulation_readiness: Literal["ready", "unresolved"]
    live_execution_readiness: Literal["ready", "unresolved"]
    research_missing_requirements: tuple[str, ...]
    live_missing_requirements: tuple[str, ...]


def _fee_ready_research(status: str | None, *, allow_provisional: bool) -> bool:
    if status == "confirmed_read_only":
        return True
    if status == "provisional_documented" and allow_provisional:
        return True
    return False


def _fee_ready_live(status: str | None) -> bool:
    return status == "confirmed_read_only"


def _order_support_research(status: str | None) -> bool:
    """Documented support is enough for research; live requires live."""
    return status in ("confirmed_read_only", "confirmed_documented")


def _order_support_live(status: str | None) -> bool:
    return status == "confirmed_read_only"


def _price_tick_source_ready_research(snapshot: SnapshotV1) -> bool:
    """Research accepts either the documented schedule OR a legacy default_tick."""
    if snapshot.price_tick_schedule_provenance is not None:
        return True
    return "default_tick" in snapshot.price_tick_rules


def check_execution_readiness(
    snapshot: SnapshotV1,
    *,
    allow_provisional_fee_model: bool,
    simulation_quantity_quantum: Decimal | None = None,
) -> ReadinessReport:
    """Return the two-surface :class:`ReadinessReport` for ``snapshot``.

    Args:
        snapshot:                    Loaded, sidecar-verified snapshot.
        allow_provisional_fee_model: Whether ``provisional_documented``
                                     fee statuses satisfy the research
                                     surface. Required — no default.
                                     Live surface never accepts
                                     provisional.
        simulation_quantity_quantum: Research accounting quantum
                                     (KRW-BTC uses ``Decimal
                                     ('0.00000001')``, the Bitcoin
                                     base unit). ``None`` deliberately
                                     leaves research readiness
                                     unresolved with the
                                     ``simulation_quantity_quantum``
                                     key surfaced — a simulator cannot
                                     floor quantities without one.

    Returns:
        A frozen :class:`ReadinessReport` whose ``*_missing_requirements``
        tuples are returned in the fixed documented order at module scope.
    """
    research_checks: dict[str, bool] = {}
    live_checks: dict[str, bool] = {}

    fee_general = snapshot.verification_status.get("general_fee_rate")
    fee_buy = snapshot.verification_status.get("market_buy_fee_reservation")
    order_buy = snapshot.verification_status.get("market_buy_price_support")
    order_sell = snapshot.verification_status.get("market_sell_market_support")
    live_accept = snapshot.verification_status.get("live_order_acceptance")

    research_checks["general_fee_rate"] = _fee_ready_research(
        fee_general, allow_provisional=allow_provisional_fee_model
    )
    research_checks["market_buy_fee_reservation"] = _fee_ready_research(
        fee_buy, allow_provisional=allow_provisional_fee_model
    )
    research_checks["price_tick_source"] = _price_tick_source_ready_research(snapshot)
    research_checks["simulation_quantity_quantum"] = (
        simulation_quantity_quantum is not None
        and simulation_quantity_quantum > 0
    )
    research_checks["fee_rates.bid"] = snapshot.fee_rates.bid is not None
    research_checks["fee_rates.ask"] = snapshot.fee_rates.ask is not None
    research_checks["krw_min_total_bid"] = (
        snapshot.minimums.krw_min_total_bid is not None
    )
    research_checks["krw_min_total_ask"] = (
        snapshot.minimums.krw_min_total_ask is not None
    )
    research_checks["market_buy_price_support"] = _order_support_research(order_buy)
    research_checks["market_sell_market_support"] = _order_support_research(order_sell)

    live_checks["general_fee_rate"] = _fee_ready_live(fee_general)
    live_checks["market_buy_fee_reservation"] = _fee_ready_live(fee_buy)
    live_checks["default_tick"] = "default_tick" in snapshot.price_tick_rules
    live_checks["default_step"] = "default_step" in snapshot.quantity_step_rules
    live_checks["fee_rates.bid"] = snapshot.fee_rates.bid is not None
    live_checks["fee_rates.ask"] = snapshot.fee_rates.ask is not None
    live_checks["krw_min_total_bid"] = (
        snapshot.minimums.krw_min_total_bid is not None
    )
    live_checks["krw_min_total_ask"] = (
        snapshot.minimums.krw_min_total_ask is not None
    )
    live_checks["market_buy_price_support"] = _order_support_live(order_buy)
    live_checks["market_sell_market_support"] = _order_support_live(order_sell)
    live_checks["live_order_acceptance"] = _fee_ready_live(live_accept)

    research_missing = tuple(
        key for key in _RESEARCH_CHECK_ORDER if not research_checks[key]
    )
    live_missing = tuple(
        key for key in _LIVE_CHECK_ORDER if not live_checks[key]
    )
    research: Literal["ready", "unresolved"] = (
        "ready" if not research_missing else "unresolved"
    )
    live: Literal["ready", "unresolved"] = (
        "ready" if not live_missing else "unresolved"
    )
    return ReadinessReport(
        research_simulation_readiness=research,
        live_execution_readiness=live,
        research_missing_requirements=research_missing,
        live_missing_requirements=live_missing,
    )


__all__ = ["ReadinessReport", "check_execution_readiness"]
