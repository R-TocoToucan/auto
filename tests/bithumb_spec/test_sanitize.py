"""Tests for :func:`bithumb_bot.bithumb_spec.sanitize.sanitize_orders_chance` (D-77, T-1-04-02)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bithumb_bot.bithumb_spec.sanitize import sanitize_orders_chance
from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse

FORBIDDEN_KEYS = (
    "access_key",
    "secret_key",
    "Authorization",
    "Set-Cookie",
    "nonce",
    "signature",
    "account_id",
    "balance",
    "avg_buy_price",
    "locked_balance",
)


def _serialize(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)


class TestSanitizerAllowlistOnly:
    def test_forbidden_top_level_keys_dropped(self) -> None:
        raw = {
            "bid_fee": "0.0025",
            "ask_fee": "0.0025",
            "market": {"name": "KRW-BTC", "order_types": []},
            # forbidden noise:
            "access_key": "ak-sentinel",
            "secret_key": "sk-sentinel",
            "Authorization": "Bearer sentinel",
            "Set-Cookie": "session=sentinel",
            "nonce": "abc",
            "signature": "sig",
            "account_id": "acct-sentinel",
            "balance": "1000000",
            "avg_buy_price": "12345",
            "locked_balance": "500",
        }
        out = sanitize_orders_chance(raw)
        for forbidden in FORBIDDEN_KEYS:
            assert forbidden not in out
        # Every sentinel value MUST be absent from the serialized output.
        serialized = _serialize(out)
        for sentinel in (
            "ak-sentinel",
            "sk-sentinel",
            "Bearer sentinel",
            "session=sentinel",
            "acct-sentinel",
        ):
            assert sentinel not in serialized, (
                f"sentinel {sentinel!r} leaked through sanitizer into "
                f"output: {serialized!r}"
            )

    def test_deeply_nested_forbidden_dropped_by_construction(self) -> None:
        """Nested forbidden keys in an unlisted parent are dropped
        because the parent itself is not in the allowlist."""
        raw = {
            "bid_fee": "0.0025",
            "ask_fee": "0.0025",
            "market": {"name": "KRW-BTC", "order_types": []},
            # Nested forbidden key inside an unlisted parent.
            "headers": {"Authorization": "sentinel-should-vanish"},
        }
        out = sanitize_orders_chance(raw)
        assert "headers" not in out
        assert "sentinel-should-vanish" not in _serialize(out)

    def test_allowlist_keys_preserved(self) -> None:
        raw = {
            "bid_fee": "0.0025",
            "ask_fee": "0.0025",
            "maker_bid_fee": "0.0020",
            "maker_ask_fee": "0.0020",
            "market": {
                "name": "KRW-BTC",
                "order_types": ["limit", "market"],
                "bid": {"currency": "KRW", "min_total": "5000"},
                "ask": {"currency": "BTC", "min_total": "0.0001"},
                "max_total": "1000000000",
            },
        }
        out = sanitize_orders_chance(raw)
        assert set(out.keys()) == {
            "bid_fee",
            "ask_fee",
            "maker_bid_fee",
            "maker_ask_fee",
            "market",
        }
        assert out["market"]["name"] == "KRW-BTC"
        assert out["market"]["order_types"] == ["limit", "market"]
        assert out["market"]["bid"] == {"currency": "KRW", "min_total": "5000"}

    def test_market_side_allowlist_only(self) -> None:
        """Forbidden keys inside `market.bid` or `market.ask` are also dropped."""
        raw = {
            "bid_fee": "0.0025",
            "ask_fee": "0.0025",
            "market": {
                "name": "KRW-BTC",
                "order_types": [],
                "bid": {
                    "currency": "KRW",
                    "min_total": "5000",
                    "account_id": "leaked-sentinel",  # forbidden
                    "balance": "leaked-2",
                },
            },
        }
        out = sanitize_orders_chance(raw)
        assert "account_id" not in out["market"]["bid"]
        assert "balance" not in out["market"]["bid"]
        assert "leaked-sentinel" not in _serialize(out)

    def test_output_validates_against_model(self) -> None:
        raw_path = (
            Path(__file__).parent.parent
            / "fixtures"
            / "bithumb"
            / "sanitized"
            / "orders_chance"
            / "example_20260908T012345Z.json"
        )
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        out = sanitize_orders_chance(raw)
        # The sanitized output MUST pass model validation.
        OrdersChanceResponse.model_validate(out)

    def test_missing_optional_maker_fees_ok(self) -> None:
        raw = {
            "bid_fee": "0.0025",
            "ask_fee": "0.0025",
            "market": {"name": "KRW-BTC", "order_types": []},
        }
        out = sanitize_orders_chance(raw)
        assert "maker_bid_fee" not in out
        assert "maker_ask_fee" not in out


class TestNeverDeepcopyAllPath:
    """T-1-04-02 discipline: no `copy.deepcopy(raw)` anywhere in the module."""

    def test_module_source_never_uses_deepcopy(self) -> None:
        import inspect

        import bithumb_bot.bithumb_spec.sanitize as mod

        src = inspect.getsource(mod)
        # Check for actual CALL (`deepcopy(`) rather than the docstring
        # mention that documents the anti-pattern.
        assert "deepcopy(" not in src, (
            "sanitize.py MUST NOT CALL copy.deepcopy — D-77 is allowlist, "
            "not blacklist"
        )
        assert "import copy" not in src
