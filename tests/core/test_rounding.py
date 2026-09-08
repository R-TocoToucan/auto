"""Tests for `bithumb_bot.core.rounding` — direction-aware quantize helpers.

Covers task 01-02-08 Behavior contract:

* `quantize_krw_amount_down(amount, unit)` — quantize DOWN to `unit`.
* `quantize_volume_down(volume, step)` — quantize DOWN to `step`.
* `quantize_price_tick_up(price, tick)` — quantize UP to `tick`
  (adverse for buy fills per D-50).
* `quantize_price_tick_down(price, tick)` — quantize DOWN to `tick`
  (adverse for sell fills per D-51).
* Non-positive `unit`/`step`/`tick` raises ValueError.
* Property tests (hypothesis) use Decimal strategies only, never floats:
  exact-multiple, tick-below, tick-above, zero, idempotence.
* Adversarial direction: `quantize_price_tick_up(1.9, 1) == 2`;
  `quantize_price_tick_down(1.9, 1) == 1`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from bithumb_bot.core.rounding import (
    quantize_krw_amount_down,
    quantize_price_tick_down,
    quantize_price_tick_up,
    quantize_volume_down,
)


# ---------------------------------------------------------------------------
# Basic direction-of-rounding smoke tests
# ---------------------------------------------------------------------------


class TestDirection:
    def test_krw_amount_rounds_down(self) -> None:
        # 199 KRW quantized to 100-unit → 100 (never 200).
        assert quantize_krw_amount_down(
            Decimal("199"), Decimal("100")
        ) == Decimal("100")

    def test_volume_rounds_down(self) -> None:
        assert quantize_volume_down(
            Decimal("0.0019"), Decimal("0.001")
        ) == Decimal("0.001")

    def test_price_tick_up_rounds_up(self) -> None:
        # 1.9 → 2 with tick=1 (adverse for a buy fill, D-50).
        assert quantize_price_tick_up(
            Decimal("1.9"), Decimal("1")
        ) == Decimal("2")

    def test_price_tick_down_rounds_down(self) -> None:
        # 1.9 → 1 with tick=1 (adverse for a sell fill, D-51).
        assert quantize_price_tick_down(
            Decimal("1.9"), Decimal("1")
        ) == Decimal("1")

    def test_zero_input_unchanged(self) -> None:
        assert quantize_krw_amount_down(Decimal(0), Decimal("100")) == Decimal(0)
        assert quantize_volume_down(Decimal(0), Decimal("0.001")) == Decimal(0)
        assert quantize_price_tick_up(Decimal(0), Decimal("1")) == Decimal(0)
        assert quantize_price_tick_down(Decimal(0), Decimal("1")) == Decimal(0)


# ---------------------------------------------------------------------------
# Non-positive unit/step/tick guarded
# ---------------------------------------------------------------------------


class TestGuards:
    @pytest.mark.parametrize(
        "fn",
        [
            quantize_krw_amount_down,
            quantize_volume_down,
            quantize_price_tick_up,
            quantize_price_tick_down,
        ],
    )
    def test_zero_unit_raises(self, fn) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError):
            fn(Decimal("100"), Decimal(0))

    @pytest.mark.parametrize(
        "fn",
        [
            quantize_krw_amount_down,
            quantize_volume_down,
            quantize_price_tick_up,
            quantize_price_tick_down,
        ],
    )
    def test_negative_unit_raises(self, fn) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError):
            fn(Decimal("100"), Decimal("-1"))


# ---------------------------------------------------------------------------
# Return type is Decimal
# ---------------------------------------------------------------------------


class TestReturnType:
    def test_returns_decimal(self) -> None:
        r = quantize_krw_amount_down(Decimal("1000"), Decimal("100"))
        assert isinstance(r, Decimal)


# ---------------------------------------------------------------------------
# Boundary tests around exact multiples
# ---------------------------------------------------------------------------


_UNIT_TABLE = [
    Decimal("1"),
    Decimal("100"),
    Decimal("0.001"),
    Decimal("0.00000001"),
]


class TestBoundaries:
    @pytest.mark.parametrize("unit", _UNIT_TABLE)
    def test_exact_multiple_down_helpers(self, unit: Decimal) -> None:
        for k in (1, 2, 5, 100):
            x = Decimal(k) * unit
            assert quantize_krw_amount_down(x, unit) == x
            assert quantize_volume_down(x, unit) == x

    @pytest.mark.parametrize("unit", _UNIT_TABLE)
    def test_exact_multiple_up_helpers(self, unit: Decimal) -> None:
        for k in (1, 2, 5, 100):
            x = Decimal(k) * unit
            assert quantize_price_tick_up(x, unit) == x
            assert quantize_price_tick_down(x, unit) == x

    @pytest.mark.parametrize("unit", _UNIT_TABLE)
    def test_just_above_multiple_down_helpers_lose_partial(
        self, unit: Decimal
    ) -> None:
        # k*unit + epsilon (a value smaller than one tick) → k*unit for DOWN.
        for k in (1, 3, 10):
            base = Decimal(k) * unit
            eps = unit / Decimal(10)  # a fraction below one tick
            x = base + eps
            assert quantize_krw_amount_down(x, unit) == base
            assert quantize_volume_down(x, unit) == base

    @pytest.mark.parametrize("unit", _UNIT_TABLE)
    def test_just_above_multiple_up_helpers_bump_to_next(
        self, unit: Decimal
    ) -> None:
        for k in (1, 3, 10):
            base = Decimal(k) * unit
            eps = unit / Decimal(10)
            x = base + eps
            # UP: base + fractional → next multiple.
            assert quantize_price_tick_up(x, unit) == base + unit

    @pytest.mark.parametrize("unit", _UNIT_TABLE)
    def test_just_below_multiple_down_helpers(
        self, unit: Decimal
    ) -> None:
        # k*unit - epsilon (smaller than one tick) → (k-1)*unit for DOWN helpers.
        for k in (1, 3, 10):
            base = Decimal(k) * unit
            eps = unit / Decimal(10)
            x = base - eps
            assert quantize_krw_amount_down(x, unit) == base - unit
            assert quantize_volume_down(x, unit) == base - unit


# ---------------------------------------------------------------------------
# Property: idempotence
# ---------------------------------------------------------------------------


_price_strat = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000000"),
    allow_nan=False,
    allow_infinity=False,
    places=8,
)
_unit_strat = st.sampled_from(
    [
        Decimal("1"),
        Decimal("100"),
        Decimal("1000"),
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.00000001"),
    ]
)


@given(x=_price_strat, unit=_unit_strat)
def test_idempotence_krw_amount_down(x: Decimal, unit: Decimal) -> None:
    once = quantize_krw_amount_down(x, unit)
    twice = quantize_krw_amount_down(once, unit)
    assert once == twice


@given(x=_price_strat, unit=_unit_strat)
def test_idempotence_volume_down(x: Decimal, unit: Decimal) -> None:
    once = quantize_volume_down(x, unit)
    twice = quantize_volume_down(once, unit)
    assert once == twice


@given(x=_price_strat, unit=_unit_strat)
def test_idempotence_price_tick_up(x: Decimal, unit: Decimal) -> None:
    once = quantize_price_tick_up(x, unit)
    twice = quantize_price_tick_up(once, unit)
    assert once == twice


@given(x=_price_strat, unit=_unit_strat)
def test_idempotence_price_tick_down(x: Decimal, unit: Decimal) -> None:
    once = quantize_price_tick_down(x, unit)
    twice = quantize_price_tick_down(once, unit)
    assert once == twice


# ---------------------------------------------------------------------------
# Property: DOWN <= x <= UP for any (positive x, positive unit)
# ---------------------------------------------------------------------------


@given(x=_price_strat, unit=_unit_strat)
def test_down_leq_input_leq_up(x: Decimal, unit: Decimal) -> None:
    assume(x >= 0)
    down = quantize_price_tick_down(x, unit)
    up = quantize_price_tick_up(x, unit)
    assert down <= x <= up
    # And the gap between DOWN and UP is either 0 (exact multiple) or one tick.
    assert (up - down) == Decimal(0) or (up - down) == unit
