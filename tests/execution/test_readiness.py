"""Unit tests for :mod:`bithumb_bot.execution.readiness`.

Every branch of :func:`check_execution_readiness` is exercised:

* Fully-ready snapshot with strict fee policy returns ``ready`` and an
  empty missing list.
* Each missing fact contributes its exact key (and only its key) to
  ``missing_requirements``.
* ``allow_provisional_fee_model=True`` promotes ``provisional_documented``
  fee-reservation to ready without silencing any other unresolved fact.
* ``unresolved_until_M6B`` remains unresolved even with the opt-in.
* ``missing_requirements`` is returned in the fixed documented order —
  independent of dict/set iteration order.

The helper takes ``allow_provisional_fee_model`` as a required
keyword-only Boolean; there is no ``ExecutionConfig`` in the call
signature and no default value, so this test file never constructs
one just to invoke readiness.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from bithumb_bot.bithumb_spec.snapshot import FeeRates, Minimums, SnapshotV1
from bithumb_bot.execution.readiness import (
    ReadinessReport,
    check_execution_readiness,
)


def _snapshot(**overrides: Any) -> SnapshotV1:
    """Build a snapshot; overrides are applied to `verification_status`,
    `price_tick_rules`, `quantity_step_rules`, and `minimums`.
    """
    verification_status: dict[str, Any] = {
        # required schema keys — validator would reject a snapshot missing
        # any of these regardless of readiness
        "general_fee_rate": "confirmed_read_only",
        "market_buy_fee_reservation": "confirmed_read_only",
        "rounding_rejection_behavior": "unresolved_until_M6B",
        "live_order_acceptance": "unresolved_until_M6B",
        # optional readiness keys tri-state — presence with
        # `confirmed_read_only` = ready; anything else = unresolved
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
        **overrides,
    )


class TestFullyReady:
    def test_all_facts_present_returns_ready(self) -> None:
        report = check_execution_readiness(
            _snapshot(), allow_provisional_fee_model=False
        )
        assert isinstance(report, ReadinessReport)
        assert report.execution_readiness == "ready"
        assert report.missing_requirements == ()


class TestEachMissingFact:
    def test_missing_default_tick(self) -> None:
        snap = _snapshot()
        # Rebuild snapshot without default_tick.
        snap = _snapshot()
        snap = SnapshotV1(**{**snap.model_dump(), "price_tick_rules": {}})
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("default_tick",)

    def test_missing_default_step(self) -> None:
        snap = _snapshot()
        snap = SnapshotV1(**{**snap.model_dump(), "quantity_step_rules": {}})
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("default_step",)

    def test_missing_krw_min_total_bid(self) -> None:
        snap = _snapshot(minimums={"krw_min_total_bid": None})
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("krw_min_total_bid",)

    def test_missing_krw_min_total_ask(self) -> None:
        snap = _snapshot(minimums={"krw_min_total_ask": None})
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("krw_min_total_ask",)

    def test_market_buy_price_support_absent_is_unresolved(self) -> None:
        # Absence proves nothing about the venue — treated as unresolved.
        snap_dict = _snapshot().model_dump()
        snap_dict["verification_status"].pop("market_buy_price_support")
        snap = SnapshotV1(**snap_dict)
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("market_buy_price_support",)

    def test_market_sell_market_support_absent_is_unresolved(self) -> None:
        snap_dict = _snapshot().model_dump()
        snap_dict["verification_status"].pop("market_sell_market_support")
        snap = SnapshotV1(**snap_dict)
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("market_sell_market_support",)

    def test_market_buy_price_support_unresolved_status(self) -> None:
        snap = _snapshot(
            verification_status={"market_buy_price_support": "unresolved_until_M6B"}
        )
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("market_buy_price_support",)


class TestFeeVerification:
    def test_general_fee_provisional_without_opt_in_is_unresolved(self) -> None:
        snap = _snapshot(
            verification_status={"general_fee_rate": "provisional_documented"}
        )
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("general_fee_rate",)

    def test_general_fee_provisional_with_opt_in_promotes_to_ready(self) -> None:
        snap = _snapshot(
            verification_status={"general_fee_rate": "provisional_documented"}
        )
        report = check_execution_readiness(snap, allow_provisional_fee_model=True)
        assert report.execution_readiness == "ready"
        assert report.missing_requirements == ()

    def test_general_fee_unresolved_stays_unresolved_even_with_opt_in(self) -> None:
        snap = _snapshot(
            verification_status={"general_fee_rate": "unresolved_until_M6B"}
        )
        report = check_execution_readiness(snap, allow_provisional_fee_model=True)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("general_fee_rate",)

    def test_buy_fee_provisional_without_opt_in_is_unresolved(self) -> None:
        snap = _snapshot(
            verification_status={
                "market_buy_fee_reservation": "provisional_documented"
            }
        )
        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("market_buy_fee_reservation",)

    def test_buy_fee_provisional_with_opt_in_promotes_to_ready(self) -> None:
        snap = _snapshot(
            verification_status={
                "market_buy_fee_reservation": "provisional_documented"
            }
        )
        report = check_execution_readiness(snap, allow_provisional_fee_model=True)
        assert report.execution_readiness == "ready"
        assert report.missing_requirements == ()

    def test_opt_in_does_not_silence_unrelated_unresolved_facts(self) -> None:
        # Buy fee is provisional (would be silenced by opt-in), but the
        # tick is missing — that must still surface. The opt-in must
        # never bulk-silence readiness.
        snap = _snapshot(
            verification_status={
                "market_buy_fee_reservation": "provisional_documented"
            }
        )
        snap = SnapshotV1(**{**snap.model_dump(), "price_tick_rules": {}})
        report = check_execution_readiness(snap, allow_provisional_fee_model=True)
        assert report.execution_readiness == "unresolved"
        assert report.missing_requirements == ("default_tick",)


class TestObservedRealSnapshotShape:
    def test_documented_missing_set_matches_observed_shape(self) -> None:
        # The real observed KRW-BTC /v1/orders/chance response:
        #   - fee_rates: 0.0025 / 0.0025 (both present)
        #   - minimums: 5000 KRW bid, 5000 KRW ask (both present)
        #   - no price_unit → no default_tick
        #   - no quantity step exposed → no default_step
        #   - order_types = ["limit"] → market-buy `price` and
        #     market-sell `market` are NOT confirmed (absent from
        #     verification_status)
        #   - general_fee_rate = confirmed_read_only
        #   - market_buy_fee_reservation = provisional_documented
        # With ``allow_provisional_fee_model=False``:
        snap_dict = _snapshot(
            verification_status={
                "market_buy_fee_reservation": "provisional_documented",
            },
        ).model_dump()
        snap_dict["price_tick_rules"] = {}
        snap_dict["quantity_step_rules"] = {}
        snap_dict["verification_status"].pop("market_buy_price_support")
        snap_dict["verification_status"].pop("market_sell_market_support")
        snap = SnapshotV1(**snap_dict)

        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        # Fixed documented order: market_buy_fee_reservation (2),
        # default_tick (3), default_step (4), market_buy_price_support (9),
        # market_sell_market_support (10).
        assert report.missing_requirements == (
            "market_buy_fee_reservation",
            "default_tick",
            "default_step",
            "market_buy_price_support",
            "market_sell_market_support",
        )


class TestDocumentedOrderIndependenceFromDictIteration:
    def test_order_is_fixed_documented_not_set_iteration(self) -> None:
        # Force every readiness fact to be missing at once and assert
        # the returned tuple is in the documented order, not any
        # incidental dict-iteration order.
        snap_dict = _snapshot(
            verification_status={
                "general_fee_rate": "unresolved_until_M6B",
                "market_buy_fee_reservation": "unresolved_until_M6B",
            },
            minimums={"krw_min_total_bid": None, "krw_min_total_ask": None},
        ).model_dump()
        snap_dict["price_tick_rules"] = {}
        snap_dict["quantity_step_rules"] = {}
        snap_dict["verification_status"].pop("market_buy_price_support")
        snap_dict["verification_status"].pop("market_sell_market_support")
        snap = SnapshotV1(**snap_dict)

        report = check_execution_readiness(snap, allow_provisional_fee_model=False)
        # fee_rates.bid / fee_rates.ask are still present on a valid
        # SnapshotV1 (pydantic makes them non-optional), so those two
        # keys never appear in `missing`. Every other readiness key does.
        assert report.missing_requirements == (
            "general_fee_rate",
            "market_buy_fee_reservation",
            "default_tick",
            "default_step",
            "krw_min_total_bid",
            "krw_min_total_ask",
            "market_buy_price_support",
            "market_sell_market_support",
        )
