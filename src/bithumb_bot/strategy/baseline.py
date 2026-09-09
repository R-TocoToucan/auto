"""Pure ``price_over_sma`` baseline signal generator.

Called with a contiguous, strictly-ascending sequence of completed
:class:`~bithumb_bot.market_data.candles.Candle` values and a
:class:`~bithumb_bot.strategy.config.BaselineStrategyConfig`. Returns a
tuple of :class:`StrategySignal` objects — one per desired-state
transition — in chronological order.

Determinism / no-look-ahead invariants (must all hold):

1. Signal at index ``k`` uses only ``candles[0 .. i(k)]``, where
   ``i(k)`` is the index of the candle whose close boundary produced
   that signal. No later candle contributes.
2. ``generate_signals(candles[:M])`` is a byte-equal prefix of
   ``generate_signals(candles[:N])`` for any ``M <= N``. Appending
   future candles never mutates past output.
3. Two invocations with identical inputs return equal tuples.

Rule (Gate-2 frozen, 2026-09-09):

* Compute the arithmetic SMA of the most recent ``lookback_candles``
  closes, including the current completed candle.
* LONG iff ``close_t > SMA_t``. CASH otherwise. Equality is CASH.

To keep the comparison exact and avoid any Decimal-division precision
worry, the rule is evaluated as ``close_t * lookback > sum_of_closes``
— algebraically identical, entirely integer/Decimal, no rounding.
The ``sma_value`` on the emitted signal is only computed on the
candles where a transition actually fires, so the reported SMA is
correct without paying the division cost every candle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from bithumb_bot.market_data.candles import Candle
from bithumb_bot.strategy.config import BaselineStrategyConfig

TargetState = Literal["LONG", "CASH"]

_INITIAL_STATE: TargetState = "CASH"


@dataclass(frozen=True)
class StrategySignal:
    """One desired-state transition emitted by the baseline.

    Attributes:
        target_state:            ``"LONG"`` or ``"CASH"`` — the new
                                 desired position state.
        signal_ts_utc:           Close boundary of the candle that
                                 completed the SMA and triggered the
                                 transition (``open_time_utc +
                                 unit_minutes``). This is the timestamp
                                 the downstream execution core treats
                                 as the signal instant.
        source_open_time_utc:    ``open_time_utc`` of that same candle,
                                 preserved so the execution core's
                                 no-same-candle structural check can
                                 assert against it.
        unit_minutes:            The source candle's ``unit_minutes``.
        close_value:             The compared close price (Decimal).
        sma_value:               ``sum_of_closes / lookback`` at the
                                 source candle, as a Decimal quotient.
        lookback_candles:        The lookback window size used.
    """

    target_state: TargetState
    signal_ts_utc: datetime
    source_open_time_utc: datetime
    unit_minutes: int
    close_value: Decimal
    sma_value: Decimal
    lookback_candles: int


def _validate_candle_sequence(
    candles: Sequence[Candle], *, market: str, unit_minutes: int
) -> None:
    """Fail-closed input check.

    * Every candle's ``market`` matches ``config.market``.
    * Every candle's ``unit_minutes`` matches ``config.unit_minutes``.
    * The sequence is strictly ascending AND contiguous: each next
      ``open_time_utc`` must equal the previous plus one ``unit_minutes``
      step. A gap silently changes what "most recent N candles" means,
      so the strategy refuses; the caller must feed a contiguous
      series (or segment its own data across gaps).

    An empty sequence is a legitimate warm-up state, not an error.
    """
    step = timedelta(minutes=unit_minutes)
    prev_time: datetime | None = None
    for candle in candles:
        if candle.market != market:
            raise ValueError(
                f"candle market {candle.market!r} at "
                f"{candle.open_time_utc.isoformat()} does not match config "
                f"market {market!r}"
            )
        if candle.unit_minutes != unit_minutes:
            raise ValueError(
                f"candle unit_minutes {candle.unit_minutes} at "
                f"{candle.open_time_utc.isoformat()} does not match config "
                f"unit_minutes {unit_minutes}"
            )
        if prev_time is not None:
            expected = prev_time + step
            if candle.open_time_utc != expected:
                raise ValueError(
                    f"non-contiguous candle sequence at "
                    f"{candle.open_time_utc.isoformat()}: expected "
                    f"{expected.isoformat()} (previous + {unit_minutes}min)"
                )
        prev_time = candle.open_time_utc


def generate_signals(
    candles: Sequence[Candle],
    config: BaselineStrategyConfig,
) -> tuple[StrategySignal, ...]:
    """Compute the full sequence of state-transition signals.

    Args:
        candles: Contiguous, strictly-ascending completed candles.
                 An empty sequence, or fewer than
                 ``config.lookback_candles`` candles, is a valid
                 warm-up input and yields an empty result.
        config:  Frozen :class:`BaselineStrategyConfig`. All fields
                 required (no defaults) — the caller supplies the
                 exact Gate-2 numbers via
                 :meth:`BaselineStrategyConfig.production` or a
                 hand-authored test fixture.

    Returns:
        Tuple of :class:`StrategySignal` in chronological order. Emits
        one signal per desired-state transition, starting from an
        implicit initial state of ``CASH``.

    Raises:
        ValueError: input structurally invalid — candle market or
                    unit_minutes mismatch, or a non-contiguous
                    sequence. Warm-up (insufficient history) is NOT an
                    error; it returns an empty tuple.
    """
    _validate_candle_sequence(
        candles, market=config.market, unit_minutes=config.unit_minutes
    )

    lookback = config.lookback_candles
    step = timedelta(minutes=config.unit_minutes)
    lookback_d = Decimal(lookback)

    signals: list[StrategySignal] = []
    current_state: TargetState = _INITIAL_STATE

    # Rolling window sum, exact Decimal.
    running_sum = Decimal("0")

    for i, candle in enumerate(candles):
        running_sum += candle.close.value
        if i >= lookback:
            running_sum -= candles[i - lookback].close.value

        # Warm-up: need at least `lookback` candles ending at index i.
        if i + 1 < lookback:
            continue

        close_value = candle.close.value
        # Exact comparison — no division: close > sum/lookback ⇔
        # close * lookback > sum (works because lookback > 0).
        # Equality maps to CASH per the frozen spec.
        rule_state: TargetState = (
            "LONG" if close_value * lookback_d > running_sum else "CASH"
        )

        if rule_state == current_state:
            # No transition (includes: first evaluable candle with
            # rule=CASH matching the implicit initial CASH state).
            continue

        signals.append(
            StrategySignal(
                target_state=rule_state,
                signal_ts_utc=candle.open_time_utc + step,
                source_open_time_utc=candle.open_time_utc,
                unit_minutes=config.unit_minutes,
                close_value=close_value,
                sma_value=running_sum / lookback_d,
                lookback_candles=lookback,
            )
        )
        current_state = rule_state

    return tuple(signals)


__all__ = ["StrategySignal", "TargetState", "generate_signals"]
