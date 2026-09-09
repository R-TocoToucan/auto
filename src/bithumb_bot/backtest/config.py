"""Frozen backtest configuration — exactly the fields the operator listed.

No production defaults. The caller must supply every value; the
runner refuses on any bounds violation at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bithumb_bot.core.money import Money
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.strategy.config import BaselineStrategyConfig


@dataclass(frozen=True)
class BacktestConfig:
    """All fields REQUIRED; no defaults. Frozen.

    Attributes:
        starting_cash_krw:         The isolated research sleeve (NOT the
                                   user's total portfolio). Passed to the
                                   runner's initial :class:`~bithumb_bot.
                                   execution.ledger.LedgerState`.
        target_sleeve_fraction:    Fraction of ``state.cash_krw`` the
                                   runner is authorized to commit as
                                   TOTAL cash debit on a buy — i.e.,
                                   ``order_notional_krw + fee_krw``, not
                                   just pre-fee notional. Bounded by
                                   ``0 < f <= 1``. Frozen for the MVP at
                                   ``Decimal("1.0")``.
        protective_stop_fraction:  Fixed engineering-baseline distance of
                                   the protective stop from the actual
                                   entry fill price, expressed as a
                                   long-side drop. ``stop_price =
                                   entry.fill_price * (1 - f)``. Bounded
                                   by ``0 < f < 1``. Frozen for the MVP
                                   at ``Decimal("0.10")``.
        strategy:                  Frozen :class:`BaselineStrategyConfig`.
                                   The runner reads ``unit_minutes`` and
                                   ``market`` from here to validate the
                                   dataset upfront.
        execution:                 Frozen :class:`ExecutionConfig`. The
                                   runner passes this unchanged to
                                   :func:`execute_intent` and
                                   :func:`evaluate_protective_stop`.
    """

    starting_cash_krw: Money
    target_sleeve_fraction: Decimal
    protective_stop_fraction: Decimal
    strategy: BaselineStrategyConfig
    execution: ExecutionConfig

    def __post_init__(self) -> None:
        if type(self.target_sleeve_fraction) is not Decimal:  # noqa: E721
            raise TypeError(
                f"target_sleeve_fraction must be Decimal, got "
                f"{type(self.target_sleeve_fraction).__name__!r}. Construct "
                "with Decimal('1.0') — never a float (D-49)."
            )
        if type(self.protective_stop_fraction) is not Decimal:  # noqa: E721
            raise TypeError(
                f"protective_stop_fraction must be Decimal, got "
                f"{type(self.protective_stop_fraction).__name__!r}. Construct "
                "with Decimal('0.10') — never a float (D-49)."
            )
        if not (Decimal("0") < self.target_sleeve_fraction <= Decimal("1")):
            raise ValueError(
                f"target_sleeve_fraction must satisfy 0 < f <= 1, got "
                f"{self.target_sleeve_fraction}"
            )
        if not (Decimal("0") < self.protective_stop_fraction < Decimal("1")):
            raise ValueError(
                f"protective_stop_fraction must satisfy 0 < f < 1, got "
                f"{self.protective_stop_fraction}"
            )
        if self.starting_cash_krw.value <= 0:
            raise ValueError(
                f"starting_cash_krw must be > 0, got "
                f"{self.starting_cash_krw.value}"
            )


__all__ = ["BacktestConfig"]
