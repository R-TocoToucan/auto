"""Phase-2 marketable-order execution + accounting core (library-only).

Scope-locked to Research MVP §2 of ``docs/IMPLEMENTATION_SCOPE.md``:

* Signals fire at completed candle ``t``'s CLOSE boundary
  (``t.open_time_utc + unit_minutes``).
* Fills use the price of the earliest genuinely available candle whose
  ``open_time_utc >= signal_ts_utc`` — never fabricated, never
  forward-filled. Same-candle fill is impossible by the structural
  ``fill_candle.open_time_utc != intent.source_open_time_utc``
  invariant (the fill candle IS the same candle only if the dataset
  is malformed; hard assert).
* Marketable KRW buys and coin sells only.
* Conservative per-side slippage (bps, single scalar), adverse
  tick-snap, floor to qty-step, fee from the M1 spec snapshot
  (subject to that snapshot's ``verification_status`` gating), min-order
  + cash/position + ``max_notional_krw`` fail-closed enforcement.
* Deterministic: pure Decimal, no wall clock, no randomness.

Deferred (do NOT implement in this unit): protective stops,
take-profit, intrabar ordering, adverse-gap rules, strategy
indicators, parameter optimization, paper/live broker, CLI, L2,
quantity-dependent impact, additional order types.
"""

from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.execution.engine import execute_intent
from bithumb_bot.execution.intent import OrderIntent, Side
from bithumb_bot.execution.ledger import (
    ExecutionReason,
    LedgerEntry,
    LedgerState,
    TimingSemantics,
)
from bithumb_bot.execution.stop import (
    ExitReason,
    ProtectiveStop,
    StopEvaluation,
    evaluate_protective_stop,
)

__all__ = [
    "ExecutionConfig",
    "ExecutionReason",
    "ExitReason",
    "LedgerEntry",
    "LedgerState",
    "OrderIntent",
    "ProtectiveStop",
    "Side",
    "StopEvaluation",
    "TimingSemantics",
    "evaluate_protective_stop",
    "execute_intent",
]
