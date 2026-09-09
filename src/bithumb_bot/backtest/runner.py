"""Chronological backtest runner — pure, deterministic, no I/O.

Event order per candle (per the Gate-2 backtest-policy freeze):

1. At candle open: execute the pending intent, if any (fills against
   the current candle's open, per :func:`execute_intent`).
2. If a BUY fills: build the full-position protective stop immediately.
3. If a SELL fills: clear the active stop.
4. Evaluate the active protective stop against the current candle.
5. If the stop triggers: append the sell entry to state, clear the
   stop, enter the stopped-out lockout.
6. At candle close: consume the baseline transition signal (if any)
   whose ``source_open_time_utc`` matches this candle.
7. LONG transition while flat AND not locked out AND no pending buy →
   create a full-sleeve buy intent for the NEXT candle.
8. CASH transition:
   * with a position → full-position sell intent for the next candle;
   * already flat AND locked out → clear the lockout;
   * else → no-op (strategy already emits only on transitions, so
     "no duplicate" is largely intrinsic).

No-look-ahead invariants (enforced by construction, not convention):

* :func:`execute_intent` and :func:`evaluate_protective_stop` see ONLY
  a truncated dataset view containing candles ``[0 .. i]`` at loop
  step ``i``. The view is a fresh :class:`~bithumb_bot.market_data.
  dataset.CandleDataset` built via ``model_copy`` — the underlying
  list is not mutated.
* Baseline signals are precomputed once at start (permitted because
  the strategy's future-append invariance is tested), then consumed
  strictly at their recorded close boundary.
* A pending intent created by the FINAL candle stays pending — the
  runner never calls :func:`execute_intent` for it, so
  :class:`NoNextCandleError` is not manufactured for the expected
  end-of-dataset boundary condition.

Fail-closed disposition
-----------------------
The runner returns a :class:`BacktestResult`. On a domain refusal it
populates ``invalid_reason`` (human-readable) and ``refusal_code``
(stable exception class name) and exits the loop early; prior ledger
entries are retained. Only the enumerated domain exceptions are
translated — everything else (``AssertionError``, ``TypeError``, and
any unexpected ``Exception``) propagates so bugs are visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import (
    BelowMinimumOrderError,
    InsufficientCashError,
    InsufficientPositionError,
    MissingIntervalInStopWindowError,
    NoNextCandleError,
    NotionalCapExceededError,
    SnapshotValidationError,
    UnverifiedFeeModelError,
)
from bithumb_bot.execution import (
    LedgerEntry,
    LedgerState,
    OrderIntent,
    ProtectiveStop,
    evaluate_protective_stop,
    execute_intent,
)
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.strategy import TargetState, generate_signals

# Exception classes the runner is allowed to translate into
# ``invalid_reason``. Any other exception propagates.
_DOMAIN_REFUSALS: tuple[type[Exception], ...] = (
    BelowMinimumOrderError,
    InsufficientCashError,
    InsufficientPositionError,
    MissingIntervalInStopWindowError,
    NoNextCandleError,
    NotionalCapExceededError,
    SnapshotValidationError,
    UnverifiedFeeModelError,
)


@dataclass(frozen=True)
class BacktestResult:
    """Immutable outcome of one :func:`run_backtest` invocation.

    ``entries`` is redundant with ``final_state.entries`` but explicit
    so callers reading the result don't need to know the ledger's
    internal shape.

    ``used_provisional_fee_model`` retains the buy-fee model's
    ``provisional_documented`` status if the run proceeded under an
    explicit opt-in — the reader can see the run is not "verified"
    against a real venue even when ``invalid_reason is None``.

    ``refusal_code`` carries the domain exception class name (e.g.
    ``"NotionalCapExceededError"``) alongside the human-readable
    message in ``invalid_reason`` so downstream code can dispatch on a
    stable identifier without regex-parsing the message.
    """

    final_state: LedgerState
    entries: tuple[LedgerEntry, ...]
    final_cash_krw: Money
    final_position_qty: Qty
    active_protective_stop: ProtectiveStop | None
    pending_intent: OrderIntent | None
    stopped_out_lockout: bool
    processed_first_open_utc: datetime | None
    processed_last_open_utc: datetime | None
    invalid_reason: str | None
    refusal_code: str | None
    used_provisional_fee_model: bool
    signals_generated: int = field(default=0)


def run_backtest(
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    config: BacktestConfig,
) -> BacktestResult:
    """Run the chronological backtest.

    Args:
        dataset:  Contiguous :class:`CandleDataset` covering the full
                  simulation range (SMA warm-up through the final
                  candle to process). Must have no internal missing
                  240-minute (or ``config.strategy.unit_minutes``)
                  slot; the runner refuses upfront if any gap exists.
        snapshot: :class:`SnapshotV1` supplying fees, tick, step,
                  minimums, and per-capability verification statuses.
        config:   Frozen :class:`BacktestConfig`.

    Returns:
        :class:`BacktestResult`. On a domain refusal, ``invalid_reason``
        and ``refusal_code`` are populated and the loop exits early;
        any ledger entries produced before the failure are retained.

    Raises:
        ValueError, TypeError, AssertionError, and any exception not
        listed in ``_DOMAIN_REFUSALS`` — these are treated as
        programming errors and must propagate visibly.
    """
    empty_state = LedgerState(
        cash_krw=config.starting_cash_krw,
        position_qty=Qty(Decimal("0")),
    )

    # ------------------------------------------------------------------
    # Pre-flight (all fail-closed conditions BEFORE the loop starts).
    # ------------------------------------------------------------------

    if dataset.market != config.strategy.market:
        raise ValueError(
            f"dataset.market={dataset.market!r} does not match "
            f"config.strategy.market={config.strategy.market!r}"
        )
    if dataset.unit_minutes != config.strategy.unit_minutes:
        raise ValueError(
            f"dataset.unit_minutes={dataset.unit_minutes} does not match "
            f"config.strategy.unit_minutes={config.strategy.unit_minutes}"
        )

    if not dataset.candles:
        return _refused(empty_state, "dataset has no candles", "EmptyDatasetError")

    # tz-aware UTC per candle
    for candle in dataset.candles:
        offset = candle.open_time_utc.utcoffset()
        if candle.open_time_utc.tzinfo is None or offset != timedelta(0):
            raise ValueError(
                f"candle.open_time_utc {candle.open_time_utc!r} is not "
                "tz-aware UTC (tzinfo required, offset must be 0)"
            )

    unit = config.strategy.unit_minutes
    step = timedelta(minutes=unit)
    first_open = dataset.candles[0].open_time_utc
    last_open = dataset.candles[-1].open_time_utc

    # Contiguity: reject any internal missing slot, whether or not it is
    # named in ``missing_intervals_utc``.
    prev = first_open
    for candle in dataset.candles[1:]:
        expected = prev + step
        if candle.open_time_utc != expected:
            return _refused(
                empty_state,
                f"internal missing candle: gap between {prev.isoformat()} "
                f"and {candle.open_time_utc.isoformat()}",
                "InternalCandleGapError",
            )
        prev = candle.open_time_utc

    # Reported missing intervals inside the internal range.
    for iso in dataset.missing_intervals_utc:
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        elif parsed.utcoffset() != timedelta(0):
            raise ValueError(
                f"missing_intervals_utc entry {iso!r} is not UTC "
                "(non-zero utcoffset)"
            )
        if first_open <= parsed <= last_open:
            return _refused(
                empty_state,
                f"reported missing interval at {parsed.isoformat()} lies "
                "inside the processed candle range",
                "ReportedMissingIntervalError",
            )

    # Buy-fee model verification pre-flight — necessary BEFORE any BUY
    # intent is sized/constructed, because a pending BUY on the final
    # candle never reaches the engine.
    buy_status = snapshot.verification_status.get("market_buy_fee_reservation")
    used_provisional = False
    if buy_status == "confirmed_read_only":
        pass
    elif buy_status == "provisional_documented":
        if not config.execution.allow_provisional_fee_model:
            return _refused(
                empty_state,
                f"snapshot.verification_status['market_buy_fee_reservation'] == "
                f"'provisional_documented' but ExecutionConfig."
                "allow_provisional_fee_model is False — refusing to size a BUY "
                "intent under unverified fee semantics",
                "UnverifiedFeeModelError",
            )
        used_provisional = True
    else:
        return _refused(
            empty_state,
            f"snapshot.verification_status['market_buy_fee_reservation'] == "
            f"{buy_status!r} — not a usable buy-fee model status",
            "UnverifiedFeeModelError",
        )

    # Snapshot must expose the fee rate we're about to use.
    fee_rate = snapshot.fee_rates.bid

    # ------------------------------------------------------------------
    # Precompute strategy signals (safe: future-append invariance is
    # tested; each signal is consumed only at its recorded boundary).
    # ------------------------------------------------------------------
    signals = generate_signals(dataset.candles, config.strategy)
    signals_by_source: dict[datetime, TargetState] = {
        s.source_open_time_utc: s.target_state for s in signals
    }

    # ------------------------------------------------------------------
    # Main loop.
    # ------------------------------------------------------------------
    state = empty_state
    pending_intent: OrderIntent | None = None
    active_stop: ProtectiveStop | None = None
    stopped_out_lockout = False
    invalid_reason: str | None = None
    refusal_code: str | None = None
    processed_last: datetime | None = None

    for i, candle in enumerate(dataset.candles):
        view = _view_up_to(dataset, i)

        # (1)–(3) Fire pending intent at this candle's open.
        if (
            pending_intent is not None
            and pending_intent.signal_ts_utc <= candle.open_time_utc
        ):
            try:
                state, entry = execute_intent(
                    state, pending_intent, view, snapshot, config.execution
                )
            except _DOMAIN_REFUSALS as exc:
                invalid_reason = str(exc)
                refusal_code = type(exc).__name__
                break
            pending_intent = None
            if entry.side == "buy":
                stop_price_d = entry.fill_price.value * (
                    Decimal("1") - config.protective_stop_fraction
                )
                active_stop = ProtectiveStop.from_entry(
                    entry, stop_price=Money(stop_price_d)
                )
            else:
                active_stop = None

        # (4)–(5) Evaluate the active stop against the CURRENT candle.
        if active_stop is not None:
            try:
                evaluation = evaluate_protective_stop(
                    state, active_stop, view, snapshot, config.execution
                )
            except _DOMAIN_REFUSALS as exc:
                invalid_reason = str(exc)
                refusal_code = type(exc).__name__
                break
            if evaluation.triggered:
                state = evaluation.new_state
                active_stop = None
                stopped_out_lockout = True

        # (6)–(8) Consume this candle's close-boundary signal.
        target = signals_by_source.get(candle.open_time_utc)
        if target == "LONG":
            if (
                state.position_qty.value == 0
                and not stopped_out_lockout
                and pending_intent is None
            ):
                target_debit = state.cash_krw.value * config.target_sleeve_fraction
                intended_pre_fee = target_debit / (Decimal("1") + fee_rate)
                if intended_pre_fee > config.execution.max_notional_krw.value:
                    invalid_reason = (
                        f"intended pre-fee order notional {intended_pre_fee} "
                        f"exceeds max_validated_notional_krw "
                        f"{config.execution.max_notional_krw.value} — "
                        "refusing to clip"
                    )
                    refusal_code = "NotionalCapExceededError"
                    processed_last = candle.open_time_utc
                    break
                pending_intent = OrderIntent.buy_from_signal(
                    candle, Money(intended_pre_fee)
                )
        elif target == "CASH":
            if state.position_qty.value > 0:
                if not (
                    pending_intent is not None and pending_intent.side == "sell"
                ):
                    pending_intent = OrderIntent.sell_from_signal(
                        candle, state.position_qty
                    )
            elif stopped_out_lockout:
                stopped_out_lockout = False

        processed_last = candle.open_time_utc

    processed_first = (
        dataset.candles[0].open_time_utc if processed_last is not None else None
    )
    return BacktestResult(
        final_state=state,
        entries=state.entries,
        final_cash_krw=state.cash_krw,
        final_position_qty=state.position_qty,
        active_protective_stop=active_stop,
        pending_intent=pending_intent,
        stopped_out_lockout=stopped_out_lockout,
        processed_first_open_utc=processed_first,
        processed_last_open_utc=processed_last,
        invalid_reason=invalid_reason,
        refusal_code=refusal_code,
        used_provisional_fee_model=used_provisional,
        signals_generated=len(signals),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _view_up_to(dataset: CandleDataset, i: int) -> CandleDataset:
    """Return a truncated view of ``dataset`` containing candles ``[0..i]``.

    Missing-interval entries beyond ``candles[i].open_time_utc`` are
    dropped so the truncated view's ``missing_intervals_utc`` only
    describes the internal range of the visible candles. This is what
    the execution engine and stop evaluator receive at loop step ``i``;
    neither can see any candle past ``i`` because the underlying list
    physically does not contain them.
    """
    last_open = dataset.candles[i].open_time_utc
    kept_missing = [
        s
        for s in dataset.missing_intervals_utc
        if _parse_utc(s) <= last_open
    ]
    return dataset.model_copy(
        update={
            "candles": dataset.candles[: i + 1],
            "missing_intervals_utc": kept_missing,
        }
    )


def _parse_utc(iso: str) -> datetime:
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _refused(
    initial_state: LedgerState, reason: str, code: str
) -> BacktestResult:
    """Build a fail-closed :class:`BacktestResult` with no entries."""
    return BacktestResult(
        final_state=initial_state,
        entries=initial_state.entries,
        final_cash_krw=initial_state.cash_krw,
        final_position_qty=initial_state.position_qty,
        active_protective_stop=None,
        pending_intent=None,
        stopped_out_lockout=False,
        processed_first_open_utc=None,
        processed_last_open_utc=None,
        invalid_reason=reason,
        refusal_code=code,
        used_provisional_fee_model=False,
        signals_generated=0,
    )


__all__ = ["BacktestResult", "run_backtest"]
