"""Minimal chronological backtest runner (library-only).

Scope-locked to Research MVP §§2–3 of ``docs/IMPLEMENTATION_SCOPE.md``
and the Gate-2 backtest-policy subset the operator froze on
2026-09-09:

* ``target_sleeve_fraction = Decimal("1.0")`` (applied to total cash
  debit including buy fee).
* ``protective_stop_fraction = Decimal("0.10")`` — 10% below the
  actual entry fill price; fixed engineering baseline, not optimized.
* ``reentry_after_stop = "wait_for_cash_then_new_long"`` — after a
  protective stop exit, remain locked in CASH while the baseline
  desired state is LONG. Rearm entry only after the baseline first
  transitions to CASH and then to LONG.

Deferred (do NOT add): metrics, reporting, charts, parameter
optimization, additional strategies, take-profit, trailing stops,
live/paper broker, WebSocket, watchdog, CLI, holdout evaluation,
generalized event engines or exchange abstractions.
"""

from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.backtest.runner import BacktestResult, run_backtest

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "run_backtest",
]
