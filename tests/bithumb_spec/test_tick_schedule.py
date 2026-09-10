"""Boundary tests for :mod:`bithumb_bot.bithumb_spec.tick_schedule`.

The official Bithumb KRW spot price-tick schedule (2026-09-10):

    price < 1                     → 0.0001
    1 <= price < 10               → 0.001
    10 <= price < 100             → 0.01
    100 <= price < 5_000          → 1
    5_000 <= price < 10_000       → 5
    10_000 <= price < 50_000      → 10
    50_000 <= price < 100_000     → 50
    100_000 <= price < 500_000    → 100
    500_000 <= price < 1_000_000  → 500
    price >= 1_000_000            → 1_000

Each band's inclusive lower bound and exclusive upper bound are
exercised. Float input is rejected (D-49). Non-positive input is
rejected.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bithumb_bot.bithumb_spec.tick_schedule import (
    KRW_TICK_LARGE,
    TICK_SCHEDULE_ACCESS_DATE_UTC,
    TICK_SCHEDULE_SHA256,
    TICK_SCHEDULE_VERSION,
    resolve_krw_tick,
    tick_schedule_provenance,
)


_BOUNDARY_CASES: list[tuple[str, str]] = [
    # (price, expected_tick)
    ("0.0000001", "0.0001"),
    ("0.999999", "0.0001"),
    ("1", "0.001"),
    ("9.999", "0.001"),
    ("10", "0.01"),
    ("99.99", "0.01"),
    ("100", "1"),
    ("4999.99", "1"),
    ("5000", "5"),
    ("9999.99", "5"),
    ("10000", "10"),
    ("49999.99", "10"),
    ("50000", "50"),
    ("99999.99", "50"),
    ("100000", "100"),
    ("499999.99", "100"),
    ("500000", "500"),
    ("999999.99", "500"),
    ("1000000", "1000"),
    ("50000000", "1000"),
    ("100000000", "1000"),
]


@pytest.mark.parametrize(("price", "expected"), _BOUNDARY_CASES)
def test_every_price_band_boundary(price: str, expected: str) -> None:
    assert resolve_krw_tick(Decimal(price)) == Decimal(expected)


class TestRejectsBadInput:
    def test_float_rejected(self) -> None:
        with pytest.raises(TypeError):
            resolve_krw_tick(1000.0)  # type: ignore[arg-type]

    def test_int_rejected(self) -> None:
        with pytest.raises(TypeError):
            resolve_krw_tick(1000)  # type: ignore[arg-type]

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_krw_tick(Decimal("0"))

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_krw_tick(Decimal("-1"))


class TestScheduleProvenance:
    def test_provenance_has_source_url_and_hash(self) -> None:
        prov = tick_schedule_provenance()
        assert prov["source_url"].startswith("https://apidocs.bithumb.com/")
        assert prov["access_date_utc"] == TICK_SCHEDULE_ACCESS_DATE_UTC
        assert prov["schedule_version"] == TICK_SCHEDULE_VERSION
        assert prov["schedule_sha256"] == TICK_SCHEDULE_SHA256
        assert len(prov["schedule_sha256"]) == 64

    def test_provenance_is_deterministic(self) -> None:
        assert tick_schedule_provenance() == tick_schedule_provenance()

    def test_large_tick_matches_last_band(self) -> None:
        assert resolve_krw_tick(Decimal("1000000")) == KRW_TICK_LARGE
        assert resolve_krw_tick(Decimal("999999999")) == KRW_TICK_LARGE
