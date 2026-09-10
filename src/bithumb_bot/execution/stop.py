"""Client-side protective sell stop — candle-only trigger detection.

Scope-locked to Research MVP §2 of ``docs/IMPLEMENTATION_SCOPE.md``:
long-only marketable exit modeled honestly against the completed
candle stream. No live monitoring, no watchdog, no take-profit, no
trailing stop.

Activation semantics
--------------------
The stop is active from ``entry.fill_ts_utc`` onward — i.e., the
entry-fill candle itself is eligible for evaluation. If its low
breaches the stop while its open was above the stop, the conservative
model is: (1) the entry filled at candle-open; (2) the protective
stop then triggered somewhere in the same candle (timing
unknowable → ``timing_semantics = within_candle_unknown``). This
avoids inventing an artificial "unprotected until next candle"
interval.

Trigger evaluation (per candle, in ascending order)
---------------------------------------------------
* ``candle.open <= stop.stop_price`` — the candle GAPPED through the
  stop. ``exit_reason = protective_stop_gap``. Base price for
  slippage = ``candle.open`` (the adverse gap opens here, not at the
  trigger level). ``timing_semantics = open_boundary`` — the fill
  instant IS the candle open to the precision we can honestly claim.
* Else if ``candle.low <= stop.stop_price`` — the candle crossed the
  stop intrabar. ``exit_reason = protective_stop_intrabar``. Base
  price = ``stop.stop_price`` (predeclared worse-outcome per
  ``docs/EXECUTION.md``: for a long protective stop the worse case is
  exiting AT the stop with adverse slippage, not near it).
  ``timing_semantics = within_candle_unknown`` — OHLC alone can't
  place the intrabar moment.
* Else the candle does not trigger; iterate.

The trigger fill goes through the shared sell path
(:func:`bithumb_bot.execution.engine._build_sell_entry`), so fee,
slippage snap, tick, step, min-order, notional-cap, position-check,
and ledger-entry construction are applied exactly once via the
authoritative implementation — never duplicated here.

Missing-interval fail-closed rule
---------------------------------
Between ``activated_at_utc`` and each candle we consider, every
expected candle slot must be present. A slot listed in
``dataset.missing_intervals_utc`` OR an unreported gap (candle
absent from the dataset without a listing) raises
:class:`~bithumb_bot.errors.MissingIntervalInStopWindowError`. The
evaluator will not silently accept a dataset that leaves an
ambiguous window across activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import (
    InsufficientPositionError,
    MissingIntervalInStopWindowError,
    NoNextCandleError,
)
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.execution.engine import (
    _build_sell_entry,
    _check_fee_verification,
    _ensure_tick_available,
    _resolve_step,
    _resolve_tick_for_price,
    _slippage_and_tick,
)
from bithumb_bot.execution.ledger import LedgerEntry, LedgerState
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset

ExitReason = Literal["gap_below_stop", "intrabar_stop_crossed"]


@dataclass(frozen=True)
class ProtectiveStop:
    """Frozen specification of a long-position protective sell stop.

    Attributes:
        stop_price:         Trigger level. Must be strictly below the
                            entry fill price — otherwise the "stop" is
                            already in the money at entry, which is not
                            a protective stop.
        qty:                Protected coin quantity. Must be > 0 and,
                            at evaluation time, ``<=`` the current
                            ``LedgerState.position_qty``. May be strictly
                            less than the entry's ``filled_qty`` for a
                            partial-protected slice — that's the caller's
                            choice, not enforced here.
        activated_at_utc:   The instant the stop becomes active. Equal
                            to ``entry.fill_ts_utc`` — the entry-fill
                            candle IS eligible for evaluation.
        unit_minutes:       Candle interval width the caller used for
                            the entry. Missing-interval and slot-walk
                            logic use this.
        entry_fill_price:   The entry's ``fill_price`` — retained so
                            construction-time validation can enforce
                            ``stop_price < entry_fill_price`` without
                            re-passing the entry.
    """

    stop_price: Money
    qty: Qty
    activated_at_utc: datetime
    unit_minutes: int
    entry_fill_price: Money

    def __post_init__(self) -> None:
        if self.stop_price.value <= 0:
            raise ValueError(
                f"stop_price must be > 0, got {self.stop_price.value}"
            )
        if self.qty.value <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty.value}")
        if self.unit_minutes <= 0:
            raise ValueError(
                f"unit_minutes must be > 0, got {self.unit_minutes}"
            )
        if self.stop_price.value >= self.entry_fill_price.value:
            raise ValueError(
                f"long protective stop requires "
                f"stop_price ({self.stop_price.value}) < "
                f"entry_fill_price ({self.entry_fill_price.value})"
            )

    @classmethod
    def from_entry(
        cls,
        entry: LedgerEntry,
        *,
        stop_price: Money,
        qty: Qty | None = None,
    ) -> ProtectiveStop:
        """Build a stop from a completed BUY entry.

        ``qty`` defaults to the entry's ``filled_qty``. ``unit_minutes``
        is derived from ``entry.signal_ts_utc - entry.source_open_time_utc``.
        """
        if entry.side != "buy":
            raise ValueError(
                f"ProtectiveStop.from_entry requires a buy entry, "
                f"got side={entry.side!r}"
            )
        delta = entry.signal_ts_utc - entry.source_open_time_utc
        unit_minutes = int(delta.total_seconds() // 60)
        return cls(
            stop_price=stop_price,
            qty=qty if qty is not None else entry.filled_qty,
            activated_at_utc=entry.fill_ts_utc,
            unit_minutes=unit_minutes,
            entry_fill_price=entry.fill_price,
        )


@dataclass(frozen=True)
class StopEvaluation:
    """Outcome of one :func:`evaluate_protective_stop` call.

    Non-triggered result: ``triggered=False``, ``fill_entry=None``,
    ``exit_reason=None``, ``new_state`` equals the input state
    unchanged, ``remaining_position_qty`` equals the current position.

    Triggered result: every optional field is populated; ``new_state``
    reflects the sell fill applied. The authoritative durable audit
    fields (exit reason, trigger candle, trigger price, fill price,
    timing semantics, protected quantity, remaining position) also
    live on ``fill_entry`` so they survive in the persisted ledger,
    not only in this transient return value.
    """

    triggered: bool
    new_state: LedgerState
    remaining_position_qty: Qty
    exit_reason: ExitReason | None = None
    trigger_candle_open_time_utc: datetime | None = None
    trigger_price: Money | None = None
    fill_entry: LedgerEntry | None = None


def evaluate_protective_stop(
    state: LedgerState,
    stop: ProtectiveStop,
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    config: ExecutionConfig,
) -> StopEvaluation:
    """Evaluate a protective stop against the candle dataset.

    Fail-closed order (before any candle is inspected):

    1. Sell-side fee-verification status via the shared engine helper.
    2. Snapshot tick + step present via the shared engine helpers.
    3. ``stop.qty <= state.position_qty`` — else
       :class:`~bithumb_bot.errors.InsufficientPositionError`.

    Then iterate eligible candles (open_time_utc >= activated_at_utc)
    slot by slot, refusing on any missing slot (reported or
    unreported), and evaluating the gap-vs-intrabar rule on each
    present candle. First trigger wins; iteration stops there.
    """
    _check_fee_verification("sell", snapshot, config)
    _ensure_tick_available(snapshot)
    step = _resolve_step(snapshot, config)

    if stop.qty.value > state.position_qty.value:
        raise InsufficientPositionError(
            f"stop.qty={stop.qty.value} exceeds current position "
            f"{state.position_qty.value}"
        )

    unit_delta = timedelta(minutes=stop.unit_minutes)
    # dataset.missing_intervals_utc is List[str] (ISO-8601 UTC per the
    # CandleDataset model). Parse once so slot comparisons are
    # datetime-to-datetime, not accidental str-to-datetime (which would
    # silently always mismatch and misroute reported gaps into the
    # "unreported" branch).
    reported_missing: frozenset[datetime] = frozenset(
        datetime.fromisoformat(s) for s in dataset.missing_intervals_utc
    )
    by_open: dict[datetime, Candle] = {c.open_time_utc: c for c in dataset.candles}

    # Fail-closed: no candle at or after activation → cannot evaluate.
    eligible_opens = [t for t in by_open if t >= stop.activated_at_utc]
    if not eligible_opens:
        raise NoNextCandleError(
            f"no candle in dataset with open_time_utc >= "
            f"{stop.activated_at_utc.isoformat()} — cannot evaluate stop"
        )
    last_open = max(eligible_opens)

    # Walk expected slots [activated_at, activated_at + unit, ...] until
    # we either trigger, run past the last available candle, or hit a
    # missing slot (reported or unreported) — the latter is fail-closed.
    slot = stop.activated_at_utc
    while slot <= last_open:
        if slot in reported_missing:
            raise MissingIntervalInStopWindowError(
                f"expected candle at {slot.isoformat()} is listed in "
                "dataset.missing_intervals_utc — cannot rule out an "
                "earlier stop trigger inside the missing window"
            )
        candle = by_open.get(slot)
        if candle is None:
            raise MissingIntervalInStopWindowError(
                f"expected candle at {slot.isoformat()} is absent from "
                "dataset.candles and NOT listed in missing_intervals_utc "
                "— refusing to treat an unreported gap as continuous"
            )

        trigger = _classify_trigger(
            candle,
            stop.stop_price.value,
            is_activation_candle=(candle.open_time_utc == stop.activated_at_utc),
        )
        if trigger is not None:
            exit_reason, base_price_d, timing = trigger
            fill_price_d = _slippage_and_tick(
                base=base_price_d,
                side="sell",
                slippage_bps=config.slippage_bps_per_side,
                tick=_resolve_tick_for_price(snapshot, base_price_d),
            )
            fill_entry = _build_sell_entry(
                state=state,
                snapshot=snapshot,
                config=config,
                source_open_time_utc=candle.open_time_utc,
                signal_ts_utc=candle.open_time_utc,
                fill_ts_utc=candle.open_time_utc,
                fill_price_d=fill_price_d,
                step=step,
                requested_qty=stop.qty,
                execution_reason=(
                    "protective_stop_gap"
                    if exit_reason == "gap_below_stop"
                    else "protective_stop_intrabar"
                ),
                timing_semantics=timing,
                trigger_price=Money(base_price_d),
            )
            new_state = state.apply(fill_entry)
            return StopEvaluation(
                triggered=True,
                new_state=new_state,
                remaining_position_qty=new_state.position_qty,
                exit_reason=exit_reason,
                trigger_candle_open_time_utc=candle.open_time_utc,
                trigger_price=Money(base_price_d),
                fill_entry=fill_entry,
            )

        slot = slot + unit_delta

    # No candle in the eligible window triggered — state unchanged.
    return StopEvaluation(
        triggered=False,
        new_state=state,
        remaining_position_qty=state.position_qty,
    )


def _classify_trigger(
    candle: Candle,
    stop_price_d: Decimal,
    *,
    is_activation_candle: bool,
) -> tuple[ExitReason, Decimal, Literal["open_boundary", "within_candle_unknown"]] | None:
    """Return ``(exit_reason, base_price, timing)`` if the candle
    triggers the stop, else ``None``.

    Activation-candle rule (correction): the stop did NOT exist before
    this candle's open, so ``candle.open <= stop_price`` CANNOT be a
    ``protective_stop_gap`` here — a gap-through implies a price move
    across a level the stop was already guarding, and it was not. On
    the activation candle only the intrabar check applies (``low <=
    stop_price`` → ``intrabar_stop_crossed``, base price = stop
    trigger level, timing unknown inside the candle).

    On later candles (``candle.open_time_utc > stop.activated_at_utc``)
    the gap check runs first: ``candle.open <= stop_price`` →
    ``gap_below_stop`` with base price = ``candle.open``. Otherwise
    the intrabar rule applies as normal.
    """
    if not is_activation_candle and candle.open.value <= stop_price_d:
        return "gap_below_stop", candle.open.value, "open_boundary"
    if candle.low.value <= stop_price_d:
        return "intrabar_stop_crossed", stop_price_d, "within_candle_unknown"
    return None


__all__ = [
    "ExitReason",
    "ProtectiveStop",
    "StopEvaluation",
    "evaluate_protective_stop",
]
