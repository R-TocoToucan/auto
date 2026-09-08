"""Tests for `bithumb_bot.core.money` — Money, Qty, and risk-denominator NewTypes.

Covers task 01-02-07 Behavior contract:

* `Money.from_str(s)` / `Qty.from_str(s)` wrap `Decimal(s)`.
* `Money(0.1)` and `Qty(0.1)` raise `TypeError` — the constructor
  NEVER accepts a float.
* `Money(some_string)` also raises (the raw constructor accepts only
  a Decimal; strings must go through `.from_str`).
* Arithmetic: `__add__`, `__sub__`, `__mul__`, comparisons defined.
* `__mul__` accepts only Decimal / int on the RHS, never float
  (raises TypeError).
* Frozen dataclass — assignment after construction raises
  FrozenInstanceError.
* Five risk-denominator NewTypes exist and each is a Decimal subtype
  at the type level. Module docstring names all five per SAFE-07.
* Hypothesis property: for any Decimal literal `d`,
  `Money.from_str(str(d)).value == d`.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import bithumb_bot.core.money as money_mod
from bithumb_bot.core.money import (
    MaxMarketLoss,
    MaxOperationalLoss,
    Money,
    PlannedStopLoss,
    PositionFraction,
    Qty,
    RiskPerTrade,
)

# ---------------------------------------------------------------------------
# Money construction discipline
# ---------------------------------------------------------------------------


class TestMoneyConstruction:
    def test_from_str_wraps_decimal(self) -> None:
        m = Money.from_str("0.1")
        assert m.value == Decimal("0.1")

    def test_from_str_supports_negative(self) -> None:
        m = Money.from_str("-0.1")
        assert m.value == Decimal("-0.1")

    def test_direct_from_decimal_ok(self) -> None:
        d = Decimal("42.5")
        m = Money(d)
        assert m.value == d

    def test_direct_from_float_raises_typeerror(self) -> None:
        with pytest.raises(TypeError):
            Money(0.1)  # type: ignore[arg-type]

    def test_direct_from_float_negative_raises(self) -> None:
        with pytest.raises(TypeError):
            Money(-0.1)  # type: ignore[arg-type]

    def test_direct_from_int_raises(self) -> None:
        # Even ints are refused — the wrapper is Decimal-only. Callers
        # must be explicit via Money.from_str("1") or Money(Decimal(1)).
        with pytest.raises(TypeError):
            Money(1)  # type: ignore[arg-type]

    def test_direct_from_string_raises(self) -> None:
        # Strings must go through .from_str explicitly.
        with pytest.raises(TypeError):
            Money("0.1")  # type: ignore[arg-type]

    def test_frozen_dataclass(self) -> None:
        m = Money.from_str("1")
        with pytest.raises(FrozenInstanceError):
            m.value = Decimal("2")  # type: ignore[misc]


class TestQtyConstruction:
    def test_from_str_wraps_decimal(self) -> None:
        q = Qty.from_str("0.001")
        assert q.value == Decimal("0.001")

    def test_direct_from_float_raises(self) -> None:
        with pytest.raises(TypeError):
            Qty(0.1)  # type: ignore[arg-type]

    def test_frozen_dataclass(self) -> None:
        q = Qty.from_str("1")
        with pytest.raises(FrozenInstanceError):
            q.value = Decimal("2")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


class TestMoneyArithmetic:
    def test_add_two_moneys(self) -> None:
        assert Money.from_str("1.5") + Money.from_str("0.25") == Money.from_str("1.75")

    def test_sub_two_moneys(self) -> None:
        assert Money.from_str("1") - Money.from_str("0.25") == Money.from_str("0.75")

    def test_mul_by_decimal(self) -> None:
        assert Money.from_str("2") * Decimal("3") == Money.from_str("6")

    def test_mul_by_int(self) -> None:
        assert Money.from_str("2") * 3 == Money.from_str("6")

    def test_mul_by_float_raises(self) -> None:
        with pytest.raises(TypeError):
            Money.from_str("2") * 3.0  # type: ignore[operator]

    def test_comparisons(self) -> None:
        a = Money.from_str("1")
        b = Money.from_str("2")
        assert a < b
        assert b > a
        assert a <= a
        assert a == Money.from_str("1")
        assert a != b


class TestQtyArithmetic:
    def test_add(self) -> None:
        assert Qty.from_str("0.5") + Qty.from_str("0.25") == Qty.from_str("0.75")

    def test_mul_by_float_raises(self) -> None:
        with pytest.raises(TypeError):
            Qty.from_str("1") * 0.5  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Property test — Decimal round-trip through .from_str
# ---------------------------------------------------------------------------


@given(
    st.decimals(
        min_value=Decimal("-1e12"),
        max_value=Decimal("1e12"),
        allow_nan=False,
        allow_infinity=False,
        places=8,
    )
)
def test_money_from_str_roundtrip(d: Decimal) -> None:
    m = Money.from_str(str(d))
    assert m.value == d


# ---------------------------------------------------------------------------
# Risk-denominator NewType vocabulary (SAFE-07)
# ---------------------------------------------------------------------------


_REQUIRED_NEWTYPES = (
    "PlannedStopLoss",
    "MaxMarketLoss",
    "MaxOperationalLoss",
    "PositionFraction",
    "RiskPerTrade",
)


class TestRiskDenominators:
    def test_every_newtype_exists(self) -> None:
        for name in _REQUIRED_NEWTYPES:
            assert hasattr(money_mod, name), f"missing NewType {name!r}"

    def test_newtypes_wrap_decimal(self) -> None:
        planned = PlannedStopLoss(Decimal("0.005"))
        assert isinstance(planned, Decimal)
        max_mkt = MaxMarketLoss(Decimal("0.02"))
        assert isinstance(max_mkt, Decimal)
        max_op = MaxOperationalLoss(Decimal("0.05"))
        assert isinstance(max_op, Decimal)
        pos = PositionFraction(Decimal("0.5"))
        assert isinstance(pos, Decimal)
        rpt = RiskPerTrade(Decimal("0.005"))
        assert isinstance(rpt, Decimal)


# ---------------------------------------------------------------------------
# Module docstring documents the SAFE-07 vocabulary
# ---------------------------------------------------------------------------


class TestModuleDocstring:
    def test_docstring_names_every_newtype(self) -> None:
        doc = (money_mod.__doc__ or "").lower()
        for name in _REQUIRED_NEWTYPES:
            assert name.lower() in doc, (
                f"{name} missing from module docstring (SAFE-07)"
            )

    def test_docstring_documents_isolated_sleeve_rule(self) -> None:
        doc = (money_mod.__doc__ or "").lower()
        # Rule from SAFE-07 / project safety rules: "the isolated 1–5%
        # sleeve — not the stop formula — is the real capital cap".
        assert "sleeve" in doc, (
            "module docstring must document the isolated sleeve rule"
        )
