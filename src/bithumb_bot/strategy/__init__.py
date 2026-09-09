"""One preregistered baseline signal generator (library-only).

Scope-locked to Research MVP §3 of ``docs/IMPLEMENTATION_SCOPE.md`` AND
the Gate-2 baseline-definition subset the operator explicitly froze on
2026-09-09:

* Rule identifier: ``price_over_sma``.
* Compute the arithmetic SMA of the most recent ``lookback_candles``
  completed native 240-minute candle CLOSES (including the current
  completed candle).
* Desired state is ``LONG`` iff ``close_t > SMA_lookback_t``;
  ``CASH`` otherwise (equality is CASH).
* Warm-up: before ``lookback_candles`` valid completed candles, no
  actionable signal — the function returns an empty tuple. It does
  NOT raise on a normal warm-up condition (only on structurally
  invalid input such as a wrong market/unit or a non-contiguous
  candle sequence).
* Initial state = CASH. At the first evaluable candle, emit a
  ``LONG`` signal only if the rule evaluates to LONG; emit nothing
  if the rule evaluates to CASH.
* Signals are emitted ONLY on desired-state transitions.
* Signal timestamp is the close boundary of the candle that
  completed the SMA computation
  (``candle.open_time_utc + unit_minutes``).
* Appending future candles does not alter any previously emitted
  signal — deterministic replay is a hard invariant of the function.

Production parameters are frozen at:
``rule_id="price_over_sma"``, ``ma_type="SMA"``,
``lookback_candles=1200``, ``warmup_candles=1200``,
``unit_minutes=240``, ``market="KRW-BTC"``.

Deferred (do NOT add here): EMA, MACD, Donchian, ADX, regime gates,
volatility targeting, parameter optimization, adaptive logic,
machine learning, order sizing, stops, fills, fees, broker code,
CLI handlers, backtest runner.
"""

from bithumb_bot.strategy.baseline import (
    StrategySignal,
    TargetState,
    generate_signals,
)
from bithumb_bot.strategy.config import BaselineStrategyConfig

__all__ = [
    "BaselineStrategyConfig",
    "StrategySignal",
    "TargetState",
    "generate_signals",
]
