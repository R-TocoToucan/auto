"""Tests for :mod:`bithumb_bot.bithumb_spec.schemas` (D-77 allowlist + D-49 StrictDecimal)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse

FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "bithumb"
    / "sanitized"
    / "orders_chance"
    / "example_20260908T012345Z.json"
)


class TestFixtureRoundTrip:
    def test_committed_fixture_validates(self) -> None:
        data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        parsed = OrdersChanceResponse.model_validate(data)
        assert parsed.bid_fee == Decimal("0.0025")
        assert parsed.ask_fee == Decimal("0.0025")
        assert parsed.market.name == "KRW-BTC"
        assert "limit" in parsed.market.order_types


class TestStrictDecimal:
    def test_json_float_rejected_for_fee(self) -> None:
        with pytest.raises(ValidationError):
            OrdersChanceResponse.model_validate(
                {
                    "bid_fee": 0.0025,  # float — must be rejected
                    "ask_fee": "0.0025",
                    "market": {"name": "KRW-BTC", "order_types": []},
                }
            )

    def test_json_string_parses_to_decimal(self) -> None:
        parsed = OrdersChanceResponse.model_validate(
            {
                "bid_fee": "0.0025",
                "ask_fee": "0.0025",
                "market": {"name": "KRW-BTC", "order_types": []},
            }
        )
        assert parsed.bid_fee == Decimal("0.0025")

    def test_maker_fees_optional(self) -> None:
        parsed = OrdersChanceResponse.model_validate(
            {
                "bid_fee": "0.0025",
                "ask_fee": "0.0025",
                "market": {"name": "KRW-BTC", "order_types": []},
            }
        )
        assert parsed.maker_bid_fee is None
        assert parsed.maker_ask_fee is None


class TestExtraForbid:
    def test_unexpected_top_level_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OrdersChanceResponse.model_validate(
                {
                    "bid_fee": "0.0025",
                    "ask_fee": "0.0025",
                    "market": {"name": "KRW-BTC", "order_types": []},
                    "unexpected_key": "surprise",
                }
            )

    def test_unexpected_market_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OrdersChanceResponse.model_validate(
                {
                    "bid_fee": "0.0025",
                    "ask_fee": "0.0025",
                    "market": {
                        "name": "KRW-BTC",
                        "order_types": [],
                        "surprise_here": 1,
                    },
                }
            )


class TestFrozen:
    def test_model_is_frozen(self) -> None:
        parsed = OrdersChanceResponse.model_validate(
            {
                "bid_fee": "0.0025",
                "ask_fee": "0.0025",
                "market": {"name": "KRW-BTC", "order_types": []},
            }
        )
        with pytest.raises(ValidationError):
            parsed.bid_fee = Decimal("0.99")  # type: ignore[misc]
