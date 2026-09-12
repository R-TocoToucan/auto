"""Bounded forward paper-trading runner (library-only).

Scope-locked to Research MVP §5 of ``docs/IMPLEMENTATION_SCOPE.md``:
consume a pre-fetched, public, immutable candle dataset; calculate
signals only on completed candles; route hypothetical orders through
the SAME accounting path as the backtest simulator
(:func:`bithumb_bot.backtest.runner.run_backtest`); persist a
restart-safe audit trail. Never loads a trade credential, never calls
a real-order endpoint, never imports :mod:`bithumb_bot.broker`.

Deferred (do NOT add here): live market-data collection, WebSocket
feeds, a watchdog, broker reconciliation, real order placement, any
fee/slippage/rounding/stop/lockout/notional-cap logic — all of that
already lives in :mod:`bithumb_bot.backtest` and
:mod:`bithumb_bot.execution` and is reused unchanged.
"""

from bithumb_bot.paper.runner import PaperSessionResult, run_paper_session
from bithumb_bot.paper.state import PaperState

__all__ = ["PaperSessionResult", "PaperState", "run_paper_session"]
