"""Research-only Donchian-style BTC breakout candidate.

Shadow candidate that runs alongside the frozen ``price_over_sma``
baseline in paper trading only. Never wired into the live-execution
path; never imported by ``bithumb_bot.broker``.

Frozen Decimal parameters (no configuration surface):

* Native completed candle unit: 240 minutes (4-hour).
* Initial state: ``CASH``.
* Entry lookback: prior ``120`` candles, EXCLUDING the current candle.
* Entry buffer: ``Decimal("50")`` bps, fixed independently of any
  execution-slippage estimate (this buffer is a strategy parameter,
  not an execution parameter).
* Exit lookback: prior ``60`` candles, EXCLUDING the current candle.

Transition rule (exact-integer form; no float, no premature division):

    Entry (CASH -> LONG):  close * 10000 > prior_120_high * (10000 + 50)
    Exit  (LONG -> CASH):  close < max(entry_breakout_level, prior_60_low)

Equality at either boundary retains the current state. Signals are
emitted ONLY on desired-state transitions. Signal timestamp is the
close boundary of the source candle (``open_time_utc + unit_minutes``),
so the execution engine's next-candle rule is honored by construction.

Determinism / no-look-ahead invariants (same guarantees as the baseline):

1. Signal at index ``k`` uses only ``candles[0..i(k)]`` (the current
   candle plus its prior 120 / 60 window).
2. ``generate_breakout_signals(candles[:M])`` is a byte-equal prefix of
   ``generate_breakout_signals(candles[:N])`` for any ``M <= N``.
3. Two invocations with identical inputs return equal tuples.

``entry_breakout_level`` is computed at entry as
``prior_120_high * Decimal("1.005")`` and carried on the emitted
LONG signal so the exit rule can reference the exact same value that
downstream persistence (state.json) will pin.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from bithumb_bot.market_data.candles import Candle

TargetState = Literal["LONG", "CASH"]

#: Frozen parameters — the operator supplies no configuration surface for
#: this shadow candidate. Bumping any of these is a source-code change.
ENTRY_LOOKBACK_CANDLES: int = 120
EXIT_LOOKBACK_CANDLES: int = 60
ENTRY_BUFFER_BPS: Decimal = Decimal("50")
BREAKOUT_MARKET: str = "KRW-BTC"
BREAKOUT_UNIT_MINUTES: int = 240

_BPS_DENOMINATOR: Decimal = Decimal("10000")
_INITIAL_STATE: TargetState = "CASH"


@dataclass(frozen=True)
class BreakoutSignal:
    """One desired-state transition from the breakout strategy.

    Attributes:
        target_state:            ``"LONG"`` or ``"CASH"``.
        signal_ts_utc:           Close boundary of the source candle
                                 (``open_time_utc + unit_minutes``).
        source_open_time_utc:    Source candle's ``open_time_utc``.
        unit_minutes:            The source candle's ``unit_minutes``.
        close_value:             The compared close (Decimal).
        prior_high:              ``prior_120_high`` at this candle (or
                                 ``None`` on CASH-exit signals, which
                                 do not need it).
        prior_low:               ``prior_60_low`` at this candle (or
                                 ``None`` on LONG-entry signals).
        entry_breakout_level:    Exact Decimal breakout threshold
                                 ``prior_120_high * Decimal("1.005")``
                                 that was crossed to enter LONG. On a
                                 LONG signal this is the level just
                                 crossed; on a CASH signal this is the
                                 level captured at the earlier entry.
    """

    target_state: TargetState
    signal_ts_utc: datetime
    source_open_time_utc: datetime
    unit_minutes: int
    close_value: Decimal
    prior_high: Decimal | None
    prior_low: Decimal | None
    entry_breakout_level: Decimal


def _validate_candles(candles: Sequence[Candle]) -> None:
    step = timedelta(minutes=BREAKOUT_UNIT_MINUTES)
    prev_time: datetime | None = None
    for candle in candles:
        if candle.market != BREAKOUT_MARKET:
            raise ValueError(
                f"candle market {candle.market!r} at "
                f"{candle.open_time_utc.isoformat()} does not match "
                f"breakout candidate's frozen market {BREAKOUT_MARKET!r}"
            )
        if candle.unit_minutes != BREAKOUT_UNIT_MINUTES:
            raise ValueError(
                f"candle unit_minutes {candle.unit_minutes} at "
                f"{candle.open_time_utc.isoformat()} does not match "
                f"breakout candidate's frozen unit_minutes "
                f"{BREAKOUT_UNIT_MINUTES}"
            )
        if prev_time is not None:
            expected = prev_time + step
            if candle.open_time_utc != expected:
                raise ValueError(
                    f"non-contiguous candle sequence at "
                    f"{candle.open_time_utc.isoformat()}: expected "
                    f"{expected.isoformat()}"
                )
        prev_time = candle.open_time_utc


def generate_breakout_signals(
    candles: Sequence[Candle],
) -> tuple[BreakoutSignal, ...]:
    """Compute all breakout state-transition signals over ``candles``.

    An empty sequence, or fewer than ``ENTRY_LOOKBACK_CANDLES + 1``
    candles, yields an empty result (warm-up).

    Raises:
        ValueError: candle market / unit_minutes mismatch, or a
                    non-contiguous sequence.
    """
    _validate_candles(candles)

    step = timedelta(minutes=BREAKOUT_UNIT_MINUTES)
    upper_factor = _BPS_DENOMINATOR + ENTRY_BUFFER_BPS  # 10000 + 50
    # `prior_120_high * 1.005` — exact for any terminating-decimal price.
    entry_multiplier: Decimal = upper_factor / _BPS_DENOMINATOR

    signals: list[BreakoutSignal] = []
    current_state: TargetState = _INITIAL_STATE
    entry_breakout_level: Decimal | None = None

    for i, candle in enumerate(candles):
        # Warm-up: need at least ENTRY_LOOKBACK prior candles for the
        # entry check. That also guarantees >= EXIT_LOOKBACK prior for
        # the exit check.
        if i < ENTRY_LOOKBACK_CANDLES:
            continue

        prior_120 = candles[i - ENTRY_LOOKBACK_CANDLES : i]
        prior_120_high = max(c.high.value for c in prior_120)
        close_value = candle.close.value

        if current_state == "CASH":
            # Entry: close * 10000 > prior_120_high * (10000 + 50).
            if close_value * _BPS_DENOMINATOR > prior_120_high * upper_factor:
                new_level = prior_120_high * entry_multiplier
                signals.append(
                    BreakoutSignal(
                        target_state="LONG",
                        signal_ts_utc=candle.open_time_utc + step,
                        source_open_time_utc=candle.open_time_utc,
                        unit_minutes=BREAKOUT_UNIT_MINUTES,
                        close_value=close_value,
                        prior_high=prior_120_high,
                        prior_low=None,
                        entry_breakout_level=new_level,
                    )
                )
                current_state = "LONG"
                entry_breakout_level = new_level
        else:  # LONG
            prior_60 = candles[i - EXIT_LOOKBACK_CANDLES : i]
            prior_60_low = min(c.low.value for c in prior_60)
            assert entry_breakout_level is not None
            exit_threshold = max(entry_breakout_level, prior_60_low)
            if close_value < exit_threshold:
                signals.append(
                    BreakoutSignal(
                        target_state="CASH",
                        signal_ts_utc=candle.open_time_utc + step,
                        source_open_time_utc=candle.open_time_utc,
                        unit_minutes=BREAKOUT_UNIT_MINUTES,
                        close_value=close_value,
                        prior_high=None,
                        prior_low=prior_60_low,
                        entry_breakout_level=entry_breakout_level,
                    )
                )
                current_state = "CASH"
                entry_breakout_level = None

    return tuple(signals)


__all__ = [
    "BREAKOUT_MARKET",
    "BREAKOUT_UNIT_MINUTES",
    "BreakoutSignal",
    "ENTRY_BUFFER_BPS",
    "ENTRY_LOOKBACK_CANDLES",
    "EXIT_LOOKBACK_CANDLES",
    "TargetState",
    "generate_breakout_signals",
]
