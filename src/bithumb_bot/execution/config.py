"""Runtime execution config — small, frozen, Decimal-only."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bithumb_bot.core.money import Money


@dataclass(frozen=True)
class ExecutionConfig:
    """Per-run knobs for :func:`bithumb_bot.execution.engine.execute_intent`.

    Attributes:
        slippage_bps_per_side:      Conservative single-scalar per-side
                                    slippage in basis points, applied
                                    symmetrically (buy pays more, sell
                                    receives less). MUST be ``Decimal``,
                                    ``>= 0``. Not a range or scenario
                                    grid — this unit implements only the
                                    single conservative bound.
        max_notional_krw:           Applicability cap for the slippage
                                    model. Compared against
                                    ``order_notional_krw`` (buy) and
                                    ``gross_proceeds_krw`` (sell). The
                                    caller sources this from Gate 1's
                                    ``max_validated_notional_krw`` once
                                    frozen, or from
                                    ``provisional_engineering_notional_krw``
                                    beforehand — the engine never
                                    picks between them.
        allow_provisional_fee_model:
                                    Explicit opt-in for using the engine
                                    against a snapshot whose relevant
                                    ``verification_status`` value is
                                    ``"provisional_documented"``.
                                    ``False`` for strategy evaluation;
                                    ``True`` only for engine tests with
                                    a conservative fixture. Has no
                                    effect when status is
                                    ``"unresolved_until_M6B"`` or
                                    ``"contradicted"`` — those are hard
                                    refused regardless.
    """

    slippage_bps_per_side: Decimal
    max_notional_krw: Money
    allow_provisional_fee_model: bool = False
    #: Research-only accounting quantum for base-asset quantities (Batch
    #: 1B). When set, :func:`~bithumb_bot.execution.engine.execute_intent`
    #: floors buy/sell quantities to this quantum instead of the
    #: snapshot's ``default_step`` — and the residual is retained as
    #: dust. This is NOT a claim about Bithumb's accepted live order
    #: step (still unresolved until M6B); for KRW-BTC the caller passes
    #: ``Decimal("0.00000001")`` (the Bitcoin base accounting unit).
    #: ``None`` = legacy path: snapshot ``default_step`` is required.
    simulation_quantity_quantum: Decimal | None = None

    def __post_init__(self) -> None:
        if type(self.slippage_bps_per_side) is not Decimal:  # noqa: E721
            raise TypeError(
                f"slippage_bps_per_side must be Decimal, got "
                f"{type(self.slippage_bps_per_side).__name__!r}. "
                "Construct with Decimal('50') — never a float (D-49)."
            )
        if self.slippage_bps_per_side < 0:
            raise ValueError(
                f"slippage_bps_per_side must be >= 0, got "
                f"{self.slippage_bps_per_side}"
            )
        if self.max_notional_krw.value <= 0:
            raise ValueError(
                f"max_notional_krw must be > 0, got {self.max_notional_krw.value}"
            )
        if self.simulation_quantity_quantum is not None:
            if type(self.simulation_quantity_quantum) is not Decimal:  # noqa: E721
                raise TypeError(
                    f"simulation_quantity_quantum must be Decimal, got "
                    f"{type(self.simulation_quantity_quantum).__name__!r}. "
                    "Construct with Decimal('0.00000001') — never a float (D-49)."
                )
            if self.simulation_quantity_quantum <= 0:
                raise ValueError(
                    f"simulation_quantity_quantum must be > 0, got "
                    f"{self.simulation_quantity_quantum}"
                )


__all__ = ["ExecutionConfig"]
