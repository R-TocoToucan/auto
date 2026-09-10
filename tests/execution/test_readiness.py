"""Unit tests for :mod:`bithumb_bot.execution.readiness`.

Batch 1B splits :class:`ReadinessReport` into two surfaces —
``research_simulation_readiness`` and ``live_execution_readiness``.
Research surface accepts documented order-type support and the
official price-tick schedule; live surface stays strict.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from bithumb_bot.bithumb_spec.snapshot import FeeRates, Minimums, SnapshotV1
from bithumb_bot.bithumb_spec.tick_schedule import tick_schedule_provenance
from bithumb_bot.execution.readiness import (
    ReadinessReport,
    check_execution_readiness,
)


_RESEARCH_QUANTUM = Decimal("0.00000001")


def _snapshot(**overrides: Any) -> SnapshotV1:
    """Build a snapshot; overrides land on verification_status,
    price_tick_rules, quantity_step_rules, minimums, or
    price_tick_schedule_provenance.
    """
    verification_status: dict[str, Any] = {
        "general_fee_rate": "confirmed_read_only",
        "market_buy_fee_reservation": "confirmed_read_only",
        "rounding_rejection_behavior": "unresolved_until_M6B",
        "live_order_acceptance": "confirmed_read_only",
        "market_buy_price_support": "confirmed_read_only",
        "market_sell_market_support": "confirmed_read_only",
    }
    verification_status.update(overrides.pop("verification_status", {}))
    price_tick = {"default_tick": Decimal("1000")}
    price_tick.update(overrides.pop("price_tick_rules", {}))
    qty_step = {"default_step": Decimal("0.001")}
    qty_step.update(overrides.pop("quantity_step_rules", {}))
    minimums_kwargs = {
        "krw_min_total_bid": Decimal("5000"),
        "krw_min_total_ask": Decimal("5000"),
    }
    minimums_kwargs.update(overrides.pop("minimums", {}))
    schedule = overrides.pop(
        "price_tick_schedule_provenance", tick_schedule_provenance()
    )
    return SnapshotV1(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        retrieved_at_utc="2026-09-09T00:00:00Z",
        source_endpoints=["/v1/orders/chance"],
        fee_rates=FeeRates(bid="0.0025", ask="0.0025"),
        minimums=Minimums(**minimums_kwargs),  # type: ignore[arg-type]
        price_tick_rules=price_tick,
        quantity_step_rules=qty_step,
        supported_order_types=["limit"],
        verification_status=verification_status,
        source_fixture_hashes=["0" * 64],
        price_tick_schedule_provenance=schedule,
        **overrides,
    )


class TestFullyReadyBothSurfaces:
    def test_all_facts_present_both_ready(self) -> None:
        report = check_execution_readiness(
            _snapshot(),
            allow_provisional_fee_model=False,
            simulation_quantity_quantum=_RESEARCH_QUANTUM,
        )
        assert isinstance(report, ReadinessReport)
        assert report.research_simulation_readiness == "ready"
        assert report.live_execution_readiness == "ready"
        assert report.research_missing_requirements == ()
        assert report.live_missing_requirements == ()


class TestResearchSurfaceRelaxations:
    def test_documented_order_support_satisfies_research(self) -> None:
        snap = _snapshot(
            verification_status={
                "market_buy_price_support": "confirmed_documented",
                "market_sell_market_support": "confirmed_documented",
            }
        )
        report = check_execution_readiness(
            snap,
            allow_provisional_fee_model=True,
            simulation_quantity_quantum=_RESEARCH_QUANTUM,
        )
        assert report.research_simulation_readiness == "ready"
        # Live requires confirmed_read_only for order support.
        assert report.live_execution_readiness == "unresolved"
        assert "market_buy_price_support" in report.live_missing_requirements
        assert "market_sell_market_support" in report.live_missing_requirements

    def test_provisional_buy_fee_with_opt_in_promotes_research_not_live(
        self,
    ) -> None:
        snap = _snapshot(
            verification_status={
                "market_buy_fee_reservation": "provisional_documented"
            }
        )
        report = check_execution_readiness(
            snap,
            allow_provisional_fee_model=True,
            simulation_quantity_quantum=_RESEARCH_QUANTUM,
        )
        assert report.research_simulation_readiness == "ready"
        assert report.live_execution_readiness == "unresolved"
        assert (
            "market_buy_fee_reservation" in report.live_missing_requirements
        )

    def test_missing_quantum_blocks_research_only(self) -> None:
        report = check_execution_readiness(
            _snapshot(),
            allow_provisional_fee_model=False,
            simulation_quantity_quantum=None,
        )
        assert report.research_simulation_readiness == "unresolved"
        assert (
            "simulation_quantity_quantum"
            in report.research_missing_requirements
        )
        # Live surface has no quantum concept; unaffected.
        assert report.live_execution_readiness == "ready"

    def test_zero_quantum_rejected_for_research(self) -> None:
        report = check_execution_readiness(
            _snapshot(),
            allow_provisional_fee_model=False,
            simulation_quantity_quantum=Decimal("0"),
        )
        assert report.research_simulation_readiness == "unresolved"
        assert (
            "simulation_quantity_quantum"
            in report.research_missing_requirements
        )


class TestLiveSurfaceStrictness:
    def test_documented_order_support_blocks_live(self) -> None:
        snap = _snapshot(
            verification_status={
                "market_buy_price_support": "confirmed_documented",
                "market_sell_market_support": "confirmed_documented",
            }
        )
        report = check_execution_readiness(
            snap,
            allow_provisional_fee_model=False,
            simulation_quantity_quantum=_RESEARCH_QUANTUM,
        )
        assert report.live_execution_readiness == "unresolved"
        assert report.live_missing_requirements[:2] == (
            # order preserved from _LIVE_CHECK_ORDER
            "market_buy_price_support",
            "market_sell_market_support",
        )

    def test_provisional_buy_fee_blocks_live_regardless_of_opt_in(self) -> None:
        snap = _snapshot(
            verification_status={
                "market_buy_fee_reservation": "provisional_documented"
            }
        )
        report = check_execution_readiness(
            snap,
            allow_provisional_fee_model=True,
            simulation_quantity_quantum=_RESEARCH_QUANTUM,
        )
        assert report.live_execution_readiness == "unresolved"
        assert (
            "market_buy_fee_reservation" in report.live_missing_requirements
        )

    def test_missing_default_step_blocks_live_only(self) -> None:
        snap_dict = _snapshot().model_dump()
        snap_dict["quantity_step_rules"] = {}
        snap = SnapshotV1(**snap_dict)
        report = check_execution_readiness(
            snap,
            allow_provisional_fee_model=False,
            simulation_quantity_quantum=_RESEARCH_QUANTUM,
        )
        # Research falls back to the schedule + research quantum.
        assert report.research_simulation_readiness == "ready"
        assert report.live_execution_readiness == "unresolved"
        assert "default_step" in report.live_missing_requirements

    def test_live_order_acceptance_unresolved_blocks_live(self) -> None:
        snap = _snapshot(
            verification_status={
                "live_order_acceptance": "unresolved_until_M6B"
            }
        )
        report = check_execution_readiness(
            snap,
            allow_provisional_fee_model=False,
            simulation_quantity_quantum=_RESEARCH_QUANTUM,
        )
        # Research surface doesn't inspect live_order_acceptance.
        assert report.research_simulation_readiness == "ready"
        assert report.live_execution_readiness == "unresolved"
        assert (
            "live_order_acceptance" in report.live_missing_requirements
        )


class TestResearchMissingOrderIsDocumented:
    def test_research_missing_order_matches_documented_order(self) -> None:
        # Force every research fact to be missing at once and assert
        # the order matches _RESEARCH_CHECK_ORDER, not any incidental
        # dict-iteration order.
        snap_dict = _snapshot(
            verification_status={
                "general_fee_rate": "unresolved_until_M6B",
                "market_buy_fee_reservation": "unresolved_until_M6B",
            },
            minimums={"krw_min_total_bid": None, "krw_min_total_ask": None},
        ).model_dump()
        snap_dict["price_tick_rules"] = {}
        snap_dict["price_tick_schedule_provenance"] = None
        snap_dict["verification_status"].pop("market_buy_price_support")
        snap_dict["verification_status"].pop("market_sell_market_support")
        snap = SnapshotV1(**snap_dict)

        report = check_execution_readiness(
            snap,
            allow_provisional_fee_model=False,
            simulation_quantity_quantum=None,
        )
        assert report.research_missing_requirements == (
            "general_fee_rate",
            "market_buy_fee_reservation",
            "price_tick_source",
            "simulation_quantity_quantum",
            "krw_min_total_bid",
            "krw_min_total_ask",
            "market_buy_price_support",
            "market_sell_market_support",
        )
