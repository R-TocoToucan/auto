"""Immutable ledger — one entry per executed intent.

Every field in Research MVP §2 item 6 is present on every entry.
Side-inapplicable fields carry ``Money(Decimal('0'))`` / ``Qty(Decimal('0'))``
rather than ``None`` so downstream consumers can sum/aggregate without
Optional gymnastics — the ``side`` field tells you which subset was
populated.

Distinct-concept discipline (correction #3):

* ``requested_notional_krw`` (buy) — strategy's request; unchanged.
* ``order_notional_krw``          — pre-fee KRW actually deployed,
                                    equal to ``fill_price * filled_qty``.
                                    On buys this may be < requested when
                                    qty-step flooring reduces the fill.
                                    On sells this equals ``gross_proceeds_krw``.
* ``net_acquired_coin`` (buy)     — coin credited to the position.
* ``requested_qty`` (sell)        — strategy's requested coin qty.
* ``filled_qty``                  — actual coin transacted (may be <
                                    requested_qty after step floor).
* ``gross_proceeds_krw`` (sell)   — pre-fee KRW proceeds.
* ``net_proceeds_krw`` (sell)     — post-fee KRW credited to cash.
* ``fee_krw``                     — the single fee applied (bid or ask).
* ``total_cash_debit_krw`` (buy)  — ``order_notional_krw + fee_krw``.

Cash/position pre-images are recorded so the entry alone is enough to
reconstruct the state transition — the ledger is auditable without
replaying the whole sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import Side

# Discriminator for why the entry exists. Ordinary strategy-signal
# entries default to "strategy_signal" so pre-existing call sites are
# unchanged. Protective-stop exits set the specific variant so a
# reader of the persisted ledger can distinguish an intrabar exit
# (whose intrabar clock is unknown) from a gap exit (whose fill price
# is anchored to the trigger candle's open).
ExecutionReason = Literal[
    "strategy_signal",
    "protective_stop_gap",
    "protective_stop_intrabar",
]

# What the timestamps on the entry actually mean.
# * ``open_boundary`` — the fill_ts_utc is the fill instant to
#   candle-open precision (strategy signals, gap exits).
# * ``within_candle_unknown`` — the fill happened somewhere inside the
#   trigger candle's [open, open+unit) window; the exact intrabar
#   moment is not knowable from OHLC alone and the reader MUST treat
#   fill_ts_utc as a candle identifier, not a wall-clock fill time.
TimingSemantics = Literal["open_boundary", "within_candle_unknown"]


@dataclass(frozen=True)
class LedgerEntry:
    side: Side

    # timing
    source_open_time_utc: datetime
    signal_ts_utc: datetime
    fill_ts_utc: datetime

    # fill pricing
    fill_price: Money  # tick-snapped, slippage-adjusted

    # request → actual reduction path (distinct concepts)
    requested_notional_krw: Money  # buy only; Money(0) for sell
    requested_qty: Qty              # sell only; Qty(0) for buy
    order_notional_krw: Money      # pre-fee KRW (both sides)
    filled_qty: Qty                 # actual coin transacted (both sides)
    net_acquired_coin: Qty         # buy only; Qty(0) for sell

    # sell-side breakdown
    gross_proceeds_krw: Money      # sell only; Money(0) for buy
    net_proceeds_krw: Money        # sell only; Money(0) for buy

    # fee + cash-flow
    fee_krw: Money
    total_cash_debit_krw: Money    # buy only; Money(0) for sell

    # state snapshots
    cash_before_krw: Money
    cash_after_krw: Money
    position_before_qty: Qty
    position_after_qty: Qty

    # ------------------------------------------------------------------
    # Durable audit metadata (defaults keep ordinary strategy-signal
    # buys/sells byte-compatible with earlier callers).
    # ------------------------------------------------------------------
    execution_reason: ExecutionReason = "strategy_signal"
    timing_semantics: TimingSemantics = "open_boundary"
    #: Populated ONLY for stop-triggered entries. For an intrabar exit
    #: this is the ``stop_price`` (the trigger level itself); for a gap
    #: exit this is the trigger candle's open. ``None`` for ordinary
    #: strategy-signal entries.
    trigger_price: Money | None = None


@dataclass(frozen=True)
class LedgerState:
    """Immutable cash/position + append-only entry list.

    ``apply(entry)`` returns a NEW ``LedgerState`` with ``cash_krw`` and
    ``position_qty`` taken from the entry's ``*_after`` snapshots. The
    engine constructs entries whose ``*_after`` values already reflect
    the transition — no arithmetic happens inside :meth:`apply`.
    """

    cash_krw: Money
    position_qty: Qty
    entries: tuple[LedgerEntry, ...] = field(default_factory=tuple)

    def apply(self, entry: LedgerEntry) -> LedgerState:
        if entry.cash_before_krw != self.cash_krw:
            raise ValueError(
                f"entry.cash_before_krw {entry.cash_before_krw.value} "
                f"!= state.cash_krw {self.cash_krw.value} — engine "
                "must build entries against the current state"
            )
        if entry.position_before_qty != self.position_qty:
            raise ValueError(
                f"entry.position_before_qty {entry.position_before_qty.value} "
                f"!= state.position_qty {self.position_qty.value}"
            )
        return LedgerState(
            cash_krw=entry.cash_after_krw,
            position_qty=entry.position_after_qty,
            entries=self.entries + (entry,),
        )


__all__ = ["ExecutionReason", "LedgerEntry", "LedgerState", "TimingSemantics"]
