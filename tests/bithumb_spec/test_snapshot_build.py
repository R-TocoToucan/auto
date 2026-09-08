"""Tests for :func:`bithumb_bot.bithumb_spec.snapshot.build_snapshot` + serializer."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.snapshot import (
    SnapshotV1,
    build_snapshot,
    serialize_snapshot,
)

FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "bithumb"
    / "sanitized"
    / "orders_chance"
    / "example_20260908T012345Z.json"
)


@pytest.fixture()
def parsed_response() -> OrdersChanceResponse:
    return OrdersChanceResponse.model_validate(
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    )


class TestBuildSnapshot:
    def test_basic_shape(self, parsed_response: OrdersChanceResponse) -> None:
        snapshot = build_snapshot(
            parsed_response, market="KRW-BTC", fixture_paths=[FIXTURE_PATH]
        )
        assert snapshot.schema_version == 1
        assert snapshot.venue == "bithumb"
        assert snapshot.market == "KRW-BTC"
        assert snapshot.fee_rates.bid == Decimal("0.0025")
        assert snapshot.fee_rates.ask == Decimal("0.0025")
        assert snapshot.source_endpoints == ["/v1/orders/chance"]

    def test_default_verification_status_per_d81_d82(
        self, parsed_response: OrdersChanceResponse
    ) -> None:
        snapshot = build_snapshot(
            parsed_response, market="KRW-BTC", fixture_paths=[FIXTURE_PATH]
        )
        assert snapshot.verification_status == {
            "general_fee_rate": "confirmed_read_only",
            "market_buy_fee_reservation": "provisional_documented",
            "rounding_rejection_behavior": "unresolved_until_M6B",
            "live_order_acceptance": "unresolved_until_M6B",
        }

    def test_source_fixture_hashes_computed(
        self, parsed_response: OrdersChanceResponse
    ) -> None:
        snapshot = build_snapshot(
            parsed_response, market="KRW-BTC", fixture_paths=[FIXTURE_PATH]
        )
        assert len(snapshot.source_fixture_hashes) == 1
        assert len(snapshot.source_fixture_hashes[0]) == 64  # sha256 hex

    def test_price_tick_from_bid_side(
        self, parsed_response: OrdersChanceResponse
    ) -> None:
        snapshot = build_snapshot(
            parsed_response, market="KRW-BTC", fixture_paths=[FIXTURE_PATH]
        )
        assert snapshot.price_tick_rules.get("default_tick") == Decimal("1000")

    def test_supported_order_types_carried(
        self, parsed_response: OrdersChanceResponse
    ) -> None:
        snapshot = build_snapshot(
            parsed_response, market="KRW-BTC", fixture_paths=[FIXTURE_PATH]
        )
        assert snapshot.supported_order_types == ["limit", "price", "market"]


class TestSerializeSnapshot:
    def test_byte_identical_across_calls(
        self, parsed_response: OrdersChanceResponse
    ) -> None:
        snapshot = build_snapshot(
            parsed_response, market="KRW-BTC", fixture_paths=[FIXTURE_PATH]
        )
        assert serialize_snapshot(snapshot) == serialize_snapshot(snapshot)

    def test_decimals_serialize_as_strings(
        self, parsed_response: OrdersChanceResponse
    ) -> None:
        """D-75: every decimal as a string, never as a JSON number."""
        snapshot = build_snapshot(
            parsed_response, market="KRW-BTC", fixture_paths=[FIXTURE_PATH]
        )
        raw = serialize_snapshot(snapshot).decode("utf-8")
        # Fee rates render as quoted strings — no bare `0.0025` numeric.
        assert '"bid":"0.0025"' in raw
        assert '"ask":"0.0025"' in raw
        # No bare float literal for the fee value anywhere.
        assert "0.0025," not in raw or raw.count("0.0025,") == raw.count('"0.0025"')

    def test_round_trip_via_model_validate(
        self, parsed_response: OrdersChanceResponse
    ) -> None:
        snapshot = build_snapshot(
            parsed_response, market="KRW-BTC", fixture_paths=[FIXTURE_PATH]
        )
        serialized = serialize_snapshot(snapshot)
        parsed = SnapshotV1.model_validate(json.loads(serialized))
        assert parsed.market == snapshot.market
        assert parsed.fee_rates.bid == snapshot.fee_rates.bid
        assert parsed.verification_status == snapshot.verification_status


class TestVerificationStatusRequiredKeys:
    def test_missing_key_raises(self) -> None:
        """D-83: pydantic validator refuses if any of the 4 keys is absent."""
        with pytest.raises(Exception):
            SnapshotV1(
                schema_version=1,
                venue="bithumb",
                market="KRW-BTC",
                retrieved_at_utc="2026-09-08T01:23:45Z",
                source_endpoints=["/v1/orders/chance"],
                fee_rates={"bid": "0.0025", "ask": "0.0025"},  # type: ignore[arg-type]
                minimums={},  # type: ignore[arg-type]
                supported_order_types=[],
                verification_status={
                    "general_fee_rate": "confirmed_read_only",
                    # missing 3 required keys
                },
                source_fixture_hashes=[],
            )
