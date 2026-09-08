"""`bithumb_bot.core.money` — Money, Qty, and the risk-denominator vocabulary.

Money / Qty value objects
-------------------------
`Money` and `Qty` are frozen `dataclass` wrappers around a single
``value: Decimal`` attribute. They exist so calling code that adds a
``Money`` to a ``Qty`` fails at ``mypy --strict`` (mixing units is a
class of bug we want caught at type-check time, not at fill time).

Construction discipline (D-49)
------------------------------
Both classes REFUSE any non-``Decimal`` positional argument in the
generated ``__init__``. The only user-facing constructor is
``Money.from_str(<str>)`` / ``Qty.from_str(<str>)`` which wraps
``Decimal(<str>)`` — no ``float`` ever touches the wrapper. The AST
checker (`tools.decimal_ast_check`) enforces at commit time that this
module itself never writes ``Decimal(<float-literal>)``.

Arithmetic
----------
``__add__`` / ``__sub__`` take same-type operands (Money+Money,
Qty+Qty). ``__mul__`` takes ``Decimal | int`` on the RHS — a ``float``
RHS raises ``TypeError`` (D-49 propagates through arithmetic).
Comparisons (``<``, ``<=``, ``>``, ``>=``, ``==``, ``!=``) are defined
between same-type operands.

Risk-denominator vocabulary (SAFE-07 — the *five* separate terms)
-----------------------------------------------------------------
The project's core invariant is that **planned loss ≠ max market loss ≠
max operational loss**, and that neither is the same thing as the size
of the isolated 1–5% capital sleeve. The rule that binds them together:
**the isolated sleeve — not the stop formula — is the real capital
cap.** Every downstream sizing / risk / capital-allocation call site
consumes these terms as `NewType`s so that a value labelled
``RiskPerTrade`` cannot be silently substituted for a value labelled
``MaxOperationalLoss`` at ``mypy --strict``:

* **PlannedStopLoss**       — the loss the strategy plans to accept if
  the stop triggers as intended: entry price minus stop price times
  quantity. Contract-level, not observed.
* **MaxMarketLoss**          — the worst-case loss the *market* can
  inflict at the moment of trigger, accounting for gap risk,
  book-side depletion, and slippage past the stop. Bounded above by
  the sleeve, not by the stop formula.
* **MaxOperationalLoss**     — the worst-case loss the *operator*
  can incur including venue-side / broker-side / execution-side
  failure modes (rate-limit block, WebSocket outage during a stop,
  reconcile mismatch). Bounded above by the sleeve.
* **PositionFraction**       — the fraction of the sleeve currently
  allocated to the open position. In [0, 1]; enforced at sizing time.
* **RiskPerTrade**           — the fraction of the sleeve the trade
  is authorized to lose (PlannedStopLoss expressed as sleeve
  fraction). In [0, sleeve-cap]; enforced at sizing time.

Every one of the five is a distinct ``NewType(Decimal)``. Runtime cost:
zero — ``NewType`` is a compile-time-only annotation. Runtime shape:
the value is a plain ``Decimal`` at runtime.

Sizing pipelines added in later phases consume these types via
``def size_order(rpt: RiskPerTrade, ...) -> Qty: ...``; passing a bare
``Decimal`` fails ``mypy --strict`` and forces the caller to construct
the labelled value explicitly (``RiskPerTrade(Decimal("0.005"))``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import NewType


# ---------------------------------------------------------------------------
# Money / Qty value objects
# ---------------------------------------------------------------------------


def _require_decimal(value: object, wrapper_name: str) -> Decimal:
    """Raise TypeError unless `value` is a Decimal.

    Used from __post_init__ so ``Money(0.1)`` / ``Money("0.1")`` /
    ``Money(1)`` all fail loudly at construction time — the only
    accepted positional argument is a `Decimal` already constructed
    by the caller (typically via `.from_str`).
    """
    if type(value) is not Decimal:  # noqa: E721 — explicit type identity
        raise TypeError(
            f"{wrapper_name} accepts only `decimal.Decimal` positional "
            f"argument, got {type(value).__name__!r}. Use "
            f"{wrapper_name}.from_str(<str>) for string input (D-49)."
        )
    return value


@dataclass(frozen=True)
class Money:
    """A KRW-denominated monetary value stored as an exact `Decimal`.

    Construct via ``Money.from_str("100000")`` — the raw constructor
    accepts only a ``Decimal`` (D-49). Arithmetic mixing ``Money`` and
    ``Qty`` fails at ``mypy --strict``.
    """

    value: Decimal

    def __post_init__(self) -> None:
        _require_decimal(self.value, "Money")

    @classmethod
    def from_str(cls, s: str) -> Money:
        """Wrap ``Decimal(s)``. The exact-decimal entry point."""
        return cls(Decimal(s))

    # -- arithmetic ---------------------------------------------------------

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented 
        return Money(self.value + other.value)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented 
        return Money(self.value - other.value)

    def __mul__(self, other: Decimal | int) -> Money:
        # Reject float explicitly — Decimal * float is banned (D-49).
        if isinstance(other, bool) or not isinstance(other, (Decimal, int)):
            raise TypeError(
                f"Money.__mul__ RHS must be Decimal or int, got "
                f"{type(other).__name__!r}. Float RHS is banned (D-49)."
            )
        return Money(self.value * Decimal(other))

    # -- comparisons ---------------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented 
        return self.value < other.value

    def __le__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented 
        return self.value <= other.value

    def __gt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented 
        return self.value > other.value

    def __ge__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented 
        return self.value >= other.value


@dataclass(frozen=True)
class Qty:
    """A base-asset quantity stored as an exact `Decimal`.

    Construct via ``Qty.from_str("0.001")``. Mixing ``Qty`` and
    ``Money`` fails at ``mypy --strict``.
    """

    value: Decimal

    def __post_init__(self) -> None:
        _require_decimal(self.value, "Qty")

    @classmethod
    def from_str(cls, s: str) -> Qty:
        return cls(Decimal(s))

    # -- arithmetic ---------------------------------------------------------

    def __add__(self, other: Qty) -> Qty:
        if not isinstance(other, Qty):
            return NotImplemented 
        return Qty(self.value + other.value)

    def __sub__(self, other: Qty) -> Qty:
        if not isinstance(other, Qty):
            return NotImplemented 
        return Qty(self.value - other.value)

    def __mul__(self, other: Decimal | int) -> Qty:
        if isinstance(other, bool) or not isinstance(other, (Decimal, int)):
            raise TypeError(
                f"Qty.__mul__ RHS must be Decimal or int, got "
                f"{type(other).__name__!r}. Float RHS is banned (D-49)."
            )
        return Qty(self.value * Decimal(other))

    # -- comparisons ---------------------------------------------------------

    def __lt__(self, other: Qty) -> bool:
        if not isinstance(other, Qty):
            return NotImplemented 
        return self.value < other.value

    def __le__(self, other: Qty) -> bool:
        if not isinstance(other, Qty):
            return NotImplemented 
        return self.value <= other.value

    def __gt__(self, other: Qty) -> bool:
        if not isinstance(other, Qty):
            return NotImplemented 
        return self.value > other.value

    def __ge__(self, other: Qty) -> bool:
        if not isinstance(other, Qty):
            return NotImplemented 
        return self.value >= other.value


# ---------------------------------------------------------------------------
# Risk-denominator vocabulary (SAFE-07) — five distinct NewType(Decimal)
# ---------------------------------------------------------------------------

PlannedStopLoss = NewType("PlannedStopLoss", Decimal)
"""Contract-level planned loss if the stop triggers as intended.

Entry price minus stop price, times quantity. **Not** the max market
loss (gaps can exceed it) and **not** the sleeve cap (the sleeve caps
what any loss can consume, not what the strategy plans to lose)."""

MaxMarketLoss = NewType("MaxMarketLoss", Decimal)
"""Worst-case market-side loss including gap and slippage past the stop.

Bounded above by the isolated sleeve, not by the stop formula."""

MaxOperationalLoss = NewType("MaxOperationalLoss", Decimal)
"""Worst-case operator-side loss including venue/broker/execution failure.

Reconcile mismatch, rate-limit blocks, WebSocket outages during a stop —
these live here, not under MaxMarketLoss. Bounded above by the sleeve."""

PositionFraction = NewType("PositionFraction", Decimal)
"""Fraction of the isolated sleeve currently allocated to the open position.

In [0, 1]. Enforced at sizing time, not asserted post-hoc."""

RiskPerTrade = NewType("RiskPerTrade", Decimal)
"""Fraction of the sleeve the trade is authorized to lose.

PlannedStopLoss expressed as a sleeve fraction. In
[0, sleeve-cap]. Enforced at sizing time."""


__all__ = [
    "MaxMarketLoss",
    "MaxOperationalLoss",
    "Money",
    "PlannedStopLoss",
    "PositionFraction",
    "Qty",
    "RiskPerTrade",
]
