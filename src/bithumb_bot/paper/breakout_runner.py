"""Bounded forward paper-trading runner for the breakout shadow candidate.

Runs alongside the existing SMA-75 paper runner
(:mod:`bithumb_bot.paper.runner`) with a fully separate state
directory, ledger, fills log, signals log, and equity trace. Never
imports :mod:`bithumb_bot.broker`; never calls a live-order endpoint.

Sequence:

1. Dataset input contract — market == ``KRW-BTC``, unit == 240 min,
   at least ``WARMUP_CANDLE_COUNT + 1`` candles.
2. Incomplete-final-candle refusal (identical semantics to
   :mod:`bithumb_bot.paper.runner`).
3. Load prior :class:`BreakoutState` if ``state.json`` exists.
   Resume-time divergence checks: dataset market, unit_minutes,
   paper_start_ts_utc, starting_cash_krw. Any mismatch refuses.
4. Signal generation via :func:`bithumb_bot.strategy.breakout.
   generate_breakout_signals` over the full dataset (deterministic
   replay guarantee — future appends produce a byte-equal prefix of
   the earlier signal tuple by construction of the generator).
5. Forward loop from ``last_processed_open_utc + unit`` (fresh start:
   from ``paper_start_ts_utc``). Per candle:

   * Fire any pending intent whose ``signal_ts_utc <=`` this candle's
     open (:func:`bithumb_bot.execution.engine.execute_intent`).
   * On a fresh BUY: create a :class:`~bithumb_bot.execution.stop.
     ProtectiveStop` at ``fill_price * (1 - 0.10)`` — the frozen 10%
     drop.
   * Evaluate the active stop against the current candle (gap or
     intrabar); trigger fires a shared sell entry and arms the
     ``stopped_out_lockout``.
   * Consume this candle's strategy signal to build the next
     pending intent (LONG opens a buy for the full sleeve; CASH
     closes the whole position). ``stopped_out_lockout`` blocks a new
     LONG until the strategy first returns to CASH.

6. Persist state.json (overwrite), append new fills / signals /
   equity points to their `.jsonl` logs.

The runner reuses the authoritative
:func:`~bithumb_bot.execution.engine.execute_intent` and
:func:`~bithumb_bot.execution.stop.evaluate_protective_stop` — no fee,
slippage, tick, step, min-order, notional-cap, or stop logic lives
here. All money math is exact :class:`Decimal`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.execution.engine import execute_intent
from bithumb_bot.execution.intent import OrderIntent
from bithumb_bot.execution.ledger import LedgerEntry, LedgerState
from bithumb_bot.execution.stop import ProtectiveStop, evaluate_protective_stop
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.strategy.breakout import (
    BREAKOUT_MARKET,
    BREAKOUT_UNIT_MINUTES,
    ENTRY_BUFFER_BPS,
    ENTRY_LOOKBACK_CANDLES,
    EXIT_LOOKBACK_CANDLES,
    BreakoutSignal,
    generate_breakout_signals,
)

#: Number of pre-paper candles required before the first forward candle
#: — equal to :data:`ENTRY_LOOKBACK_CANDLES` so the strategy's first
#: evaluable candle is exactly ``paper_start_ts_utc``.
WARMUP_CANDLE_COUNT: int = ENTRY_LOOKBACK_CANDLES

_PROTECTIVE_STOP_FRACTION: Decimal = Decimal("0.10")
_TARGET_SLEEVE_FRACTION: Decimal = Decimal("1.0")
_SCHEMA_VERSION: int = 1
_RUN_PURPOSE: str = "engineering_smoke"

_DOMAIN_REFUSALS = (
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
class BreakoutPaperConfig:
    """Config surface for the shadow breakout paper runner.

    ``execution`` is passed straight through to the shared execution
    engine — the CLI handler builds it from operator-provided cash and
    cap arguments.
    """

    starting_cash_krw: Money
    max_notional_krw: Money
    execution: ExecutionConfig


@dataclass(frozen=True)
class EquityPoint:
    """Mark-to-market equity snapshot at a fill boundary."""

    candle_open_time_utc: datetime
    cash_krw: Money
    position_qty: Qty
    close_price: Money
    mark_to_market_equity_krw: Money


@dataclass(frozen=True)
class BreakoutSessionResult:
    """Outcome of one :func:`run_breakout_paper_session` invocation."""

    paper_start_ts_utc: datetime | None
    warmup_candle_count: int
    forward_candle_count: int
    forward_entries: tuple[LedgerEntry, ...]
    forward_signal_count: int
    new_fills_this_invocation: int
    new_signals_this_invocation: int
    resumed: bool
    invalid_reason: str | None
    refusal_code: str | None
    final_cash_krw: Money
    final_position_qty: Qty
    equity_curve: tuple[EquityPoint, ...]
    stopped_out_lockout: bool
    active_stop_price: Money | None
    entry_breakout_level: Decimal | None
    last_processed_open_utc: datetime | None


# ---------------------------------------------------------------------------
# state persistence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakoutState:
    """Byte-exact ``state.json`` shape. Every Decimal is stored as a string."""

    schema_version: int
    run_purpose: str
    selection_eligible: bool
    holdout_eligible: bool
    entry_buffer_bps: str
    entry_lookback_candles: int
    exit_lookback_candles: int
    dataset_market: str
    unit_minutes: int
    paper_start_ts_utc: str
    starting_cash_krw: str
    final_cash_krw: str
    final_position_qty: str
    stopped_out_lockout: bool
    active_stop_price: str | None
    entry_breakout_level: str | None
    last_processed_open_utc: str
    forward_fill_count: int
    forward_signal_count: int
    forward_candle_count: int


def _dstr(d: Decimal) -> str:
    return format(d, "f")


def _load_state(path: Path) -> BreakoutState | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return BreakoutState(**raw)


def _save_state(path: Path, state: BreakoutState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(state), indent=2, sort_keys=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------


def _serialize_ledger_entry(entry: LedgerEntry) -> dict[str, Any]:
    def _conv(value: Any) -> Any:
        if value is None or isinstance(value, (bool, str, int)):
            return value
        if isinstance(value, Decimal):
            return _dstr(value)
        if isinstance(value, (Money, Qty)):
            return _dstr(value.value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): _conv(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_conv(v) for v in value]
        return str(value)

    return _conv(asdict(entry))  # type: ignore[no-any-return]


def _serialize_signal(signal: BreakoutSignal) -> dict[str, Any]:
    return {
        "target_state": signal.target_state,
        "signal_ts_utc": signal.signal_ts_utc.isoformat(),
        "source_open_time_utc": signal.source_open_time_utc.isoformat(),
        "unit_minutes": signal.unit_minutes,
        "close_value": _dstr(signal.close_value),
        "prior_high": (
            _dstr(signal.prior_high) if signal.prior_high is not None else None
        ),
        "prior_low": (
            _dstr(signal.prior_low) if signal.prior_low is not None else None
        ),
        "entry_breakout_level": _dstr(signal.entry_breakout_level),
    }


def _serialize_equity_point(point: EquityPoint) -> dict[str, Any]:
    return {
        "candle_open_time_utc": point.candle_open_time_utc.isoformat(),
        "cash_krw": _dstr(point.cash_krw.value),
        "position_qty": _dstr(point.position_qty.value),
        "close_price": _dstr(point.close_price.value),
        "mark_to_market_equity_krw": _dstr(point.mark_to_market_equity_krw.value),
    }


# ---------------------------------------------------------------------------
# refusal helper
# ---------------------------------------------------------------------------


def _refused(
    *,
    paper_start_ts_utc: datetime | None,
    forward_candle_count: int,
    resumed: bool,
    cash: Money,
    position: Qty,
    reason: str,
    code: str,
) -> BreakoutSessionResult:
    return BreakoutSessionResult(
        paper_start_ts_utc=paper_start_ts_utc,
        warmup_candle_count=WARMUP_CANDLE_COUNT,
        forward_candle_count=forward_candle_count,
        forward_entries=(),
        forward_signal_count=0,
        new_fills_this_invocation=0,
        new_signals_this_invocation=0,
        resumed=resumed,
        invalid_reason=reason,
        refusal_code=code,
        final_cash_krw=cash,
        final_position_qty=position,
        equity_curve=(),
        stopped_out_lockout=False,
        active_stop_price=None,
        entry_breakout_level=None,
        last_processed_open_utc=None,
    )


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


class BreakoutRunnerError(Exception):
    """Base class for shadow-runner refusals surfaced to the CLI."""


class BreakoutInputContractMismatchError(BreakoutRunnerError):
    """Dataset market or unit_minutes disagrees with the breakout candidate."""


class BreakoutResumeDivergenceError(BreakoutRunnerError):
    """Resumed state.json disagrees with the current invocation's inputs."""


def run_breakout_paper_session(
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    config: BreakoutPaperConfig,
    state_dir: Path,
    *,
    now_utc: datetime,
) -> BreakoutSessionResult:
    """Run one bounded shadow-candidate forward paper session."""
    zero_money = Money(Decimal("0"))
    zero_qty = Qty(Decimal("0"))

    # ---- input-contract refusals (raised so the CLI can distinguish
    # them from soft refusals and print a clean stderr line).
    if dataset.market != BREAKOUT_MARKET:
        raise BreakoutInputContractMismatchError(
            f"dataset.market={dataset.market!r} does not match breakout "
            f"candidate's frozen market {BREAKOUT_MARKET!r}"
        )
    if dataset.unit_minutes != BREAKOUT_UNIT_MINUTES:
        raise BreakoutInputContractMismatchError(
            f"dataset.unit_minutes={dataset.unit_minutes} does not match "
            f"breakout candidate's frozen unit_minutes {BREAKOUT_UNIT_MINUTES}"
        )

    state_dir.mkdir(parents=True, exist_ok=True)
    state_json_path = state_dir / "state.json"
    fills_path = state_dir / "fills.jsonl"
    signals_path = state_dir / "signals.jsonl"
    equity_path = state_dir / "equity.jsonl"

    if len(dataset.candles) < WARMUP_CANDLE_COUNT + 1:
        return _refused(
            paper_start_ts_utc=None,
            forward_candle_count=0,
            resumed=False,
            cash=config.starting_cash_krw,
            position=zero_qty,
            reason=(
                f"dataset has {len(dataset.candles)} candles; at least "
                f"{WARMUP_CANDLE_COUNT + 1} required "
                f"({WARMUP_CANDLE_COUNT} warm-up + >=1 forward)"
            ),
            code="BreakoutInsufficientForwardCandlesError",
        )

    step = timedelta(minutes=BREAKOUT_UNIT_MINUTES)
    paper_start_ts_utc = dataset.candles[WARMUP_CANDLE_COUNT].open_time_utc

    # Incomplete-final-candle refusal.
    for candle in dataset.candles:
        close_boundary = candle.open_time_utc + step
        if close_boundary > now_utc:
            return _refused(
                paper_start_ts_utc=paper_start_ts_utc,
                forward_candle_count=len(dataset.candles) - WARMUP_CANDLE_COUNT,
                resumed=False,
                cash=config.starting_cash_krw,
                position=zero_qty,
                reason=(
                    f"candle at {candle.open_time_utc.isoformat()} has not "
                    f"closed yet (close boundary {close_boundary.isoformat()} "
                    f"> now_utc {now_utc.isoformat()}) — refusing to treat "
                    "an in-progress candle as a completed observation"
                ),
                code="BreakoutIncompleteCandleError",
            )

    prior = _load_state(state_json_path)
    resumed = prior is not None
    if prior is not None:
        _check_resume_divergence(prior, dataset, config, paper_start_ts_utc)
        if prior.final_position_qty != "0":
            # A live LONG position on the last save would require
            # rebuilding a full ProtectiveStop object across resumes.
            # This minimal shadow runner refuses that case: resume is
            # only supported when the prior invocation left the runner
            # flat (position 0). Fail-closed.
            raise BreakoutResumeDivergenceError(
                "resume across an open long position is not supported by "
                "the shadow breakout runner. Close the position or start "
                "a fresh state directory."
            )

    forward_start_ts = (
        _parse_utc(prior.last_processed_open_utc) + step
        if prior is not None
        else paper_start_ts_utc
    )

    # ------------------------------------------------------------------
    # Reduced-dataset signal generation: feed exactly ENTRY_LOOKBACK
    # pre-forward candles + every candle at or after forward_start_ts.
    # The strategy's first evaluable candle is thus exactly
    # forward_start_ts, starting in fresh CASH state. This mirrors the
    # existing SMA paper runner's reduced-dataset trick and guarantees
    # the strategy's internal state at the resume boundary matches the
    # runner's flat-position invariant.
    # ------------------------------------------------------------------
    open_time_to_idx = {c.open_time_utc: i for i, c in enumerate(dataset.candles)}
    forward_start_idx = open_time_to_idx.get(forward_start_ts)
    if forward_start_idx is None:
        # Resume asked to start at a candle absent from the current
        # dataset — either the dataset shrank or the operator lost
        # append continuity. Refuse.
        raise BreakoutResumeDivergenceError(
            f"resume expected forward_start_ts="
            f"{forward_start_ts.isoformat()} in the dataset, but no candle "
            "with that open_time_utc is present"
        )
    reduce_start_idx = forward_start_idx - ENTRY_LOOKBACK_CANDLES
    if reduce_start_idx < 0:
        raise BreakoutResumeDivergenceError(
            f"resume expected at least {ENTRY_LOOKBACK_CANDLES} pre-forward "
            f"candles, but only {forward_start_idx} are present"
        )
    reduced_candles = dataset.candles[reduce_start_idx:]
    all_signals = generate_breakout_signals(reduced_candles)
    signals_by_source: dict[datetime, BreakoutSignal] = {
        s.source_open_time_utc: s for s in all_signals
    }

    # ------------------------------------------------------------------
    # seed loop state from prior or fresh.
    # ------------------------------------------------------------------
    state = _seed_ledger_state(prior, config.starting_cash_krw)
    pending_intent: OrderIntent | None = None
    active_stop: ProtectiveStop | None = None
    stopped_out_lockout: bool = (
        prior.stopped_out_lockout if prior is not None else False
    )
    entry_breakout_level: Decimal | None = None

    new_entries: list[LedgerEntry] = []
    new_signals: list[BreakoutSignal] = []
    new_equity_points: list[EquityPoint] = []

    invalid_reason: str | None = None
    refusal_code: str | None = None
    last_processed_open: datetime | None = (
        _parse_utc(prior.last_processed_open_utc) if prior is not None else None
    )
    forward_candle_count = 0

    for i, candle in enumerate(dataset.candles):
        if candle.open_time_utc < forward_start_ts:
            continue
        forward_candle_count += 1
        view = _view_up_to(dataset, i)

        # Fire pending intent at this candle's open.
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
            new_entries.append(entry)
            new_equity_points.append(
                _equity_point(entry.fill_ts_utc, state, candle.close)
            )
            if entry.side == "buy":
                stop_price_d = entry.fill_price.value * (
                    Decimal("1") - _PROTECTIVE_STOP_FRACTION
                )
                active_stop = ProtectiveStop.from_entry(
                    entry, stop_price=Money(stop_price_d)
                )
            else:
                active_stop = None
                entry_breakout_level = None

        # Evaluate active protective stop against this candle.
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
                assert evaluation.fill_entry is not None
                state = evaluation.new_state
                active_stop = None
                stopped_out_lockout = True
                entry_breakout_level = None
                new_entries.append(evaluation.fill_entry)
                new_equity_points.append(
                    _equity_point(
                        evaluation.fill_entry.fill_ts_utc, state, candle.close
                    )
                )

        # Consume this candle's strategy signal.
        signal = signals_by_source.get(candle.open_time_utc)
        if signal is not None:
            new_signals.append(signal)
            if signal.target_state == "LONG":
                if (
                    state.position_qty.value == 0
                    and not stopped_out_lockout
                    and pending_intent is None
                ):
                    fee_rate = snapshot.fee_rates.bid
                    target_debit = (
                        state.cash_krw.value * _TARGET_SLEEVE_FRACTION
                    )
                    intended_pre_fee = target_debit / (Decimal("1") + fee_rate)
                    if intended_pre_fee > config.execution.max_notional_krw.value:
                        invalid_reason = (
                            f"intended pre-fee order notional "
                            f"{intended_pre_fee} exceeds max_notional_krw "
                            f"{config.execution.max_notional_krw.value}"
                        )
                        refusal_code = "NotionalCapExceededError"
                        break
                    pending_intent = OrderIntent.buy_from_signal(
                        candle, Money(intended_pre_fee)
                    )
                    entry_breakout_level = signal.entry_breakout_level
            else:  # CASH
                if state.position_qty.value > 0 and pending_intent is None:
                    pending_intent = OrderIntent.sell_from_signal(
                        candle, state.position_qty
                    )
                elif stopped_out_lockout:
                    stopped_out_lockout = False

        last_processed_open = candle.open_time_utc

    # -----------------------------------------------------------------
    # persist state + append audit-trail logs.
    # -----------------------------------------------------------------
    prior_fill_count = _line_count(fills_path)
    prior_signal_count = _line_count(signals_path)
    prior_forward_candle_count = (
        prior.forward_candle_count if prior is not None else 0
    )

    for entry in new_entries:
        _append_jsonl(fills_path, _serialize_ledger_entry(entry))
    for signal in new_signals:
        _append_jsonl(signals_path, _serialize_signal(signal))
    for point in new_equity_points:
        _append_jsonl(equity_path, _serialize_equity_point(point))

    saved_last_processed = (
        last_processed_open.isoformat()
        if last_processed_open is not None
        else paper_start_ts_utc.isoformat()
    )
    new_state = BreakoutState(
        schema_version=_SCHEMA_VERSION,
        run_purpose=_RUN_PURPOSE,
        selection_eligible=False,
        holdout_eligible=False,
        entry_buffer_bps=_dstr(ENTRY_BUFFER_BPS),
        entry_lookback_candles=ENTRY_LOOKBACK_CANDLES,
        exit_lookback_candles=EXIT_LOOKBACK_CANDLES,
        dataset_market=dataset.market,
        unit_minutes=dataset.unit_minutes,
        paper_start_ts_utc=paper_start_ts_utc.isoformat(),
        starting_cash_krw=_dstr(config.starting_cash_krw.value),
        final_cash_krw=_dstr(state.cash_krw.value),
        final_position_qty=_dstr(state.position_qty.value),
        stopped_out_lockout=stopped_out_lockout,
        active_stop_price=(
            _dstr(active_stop.stop_price.value) if active_stop is not None else None
        ),
        entry_breakout_level=(
            _dstr(entry_breakout_level) if entry_breakout_level is not None else None
        ),
        last_processed_open_utc=saved_last_processed,
        forward_fill_count=prior_fill_count + len(new_entries),
        forward_signal_count=prior_signal_count + len(new_signals),
        forward_candle_count=prior_forward_candle_count + forward_candle_count,
    )
    _save_state(state_json_path, new_state)

    # ------------------------------------------------------------------
    # assemble the result. `forward_entries` is the state.entries tuple
    # (all entries in the LedgerState, which reflects everything —
    # including prior-invocation fills we didn't touch, i.e. `resumed
    # is False` case only). For a resumed run, the prior entries are
    # replayed onto the seeded ledger by starting from the seeded
    # cash/position rather than the true entry history — so
    # `forward_entries` here reports the NEW entries produced this
    # invocation. Callers wanting the full audit trail read fills.jsonl.
    return BreakoutSessionResult(
        paper_start_ts_utc=paper_start_ts_utc,
        warmup_candle_count=WARMUP_CANDLE_COUNT,
        forward_candle_count=forward_candle_count,
        forward_entries=tuple(new_entries),
        forward_signal_count=len(new_signals),
        new_fills_this_invocation=len(new_entries),
        new_signals_this_invocation=len(new_signals),
        resumed=resumed,
        invalid_reason=invalid_reason,
        refusal_code=refusal_code,
        final_cash_krw=state.cash_krw,
        final_position_qty=state.position_qty,
        equity_curve=tuple(new_equity_points),
        stopped_out_lockout=stopped_out_lockout,
        active_stop_price=active_stop.stop_price if active_stop is not None else None,
        entry_breakout_level=entry_breakout_level,
        last_processed_open_utc=last_processed_open,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_ledger_state(
    prior: BreakoutState | None, starting_cash_krw: Money
) -> LedgerState:
    if prior is None:
        return LedgerState(cash_krw=starting_cash_krw, position_qty=Qty(Decimal("0")))
    return LedgerState(
        cash_krw=Money(Decimal(prior.final_cash_krw)),
        position_qty=Qty(Decimal(prior.final_position_qty)),
    )


def _check_resume_divergence(
    prior: BreakoutState,
    dataset: CandleDataset,
    config: BreakoutPaperConfig,
    paper_start_ts_utc: datetime,
) -> None:
    mismatches: list[str] = []
    if prior.dataset_market != dataset.market:
        mismatches.append("dataset_market")
    if prior.unit_minutes != dataset.unit_minutes:
        mismatches.append("unit_minutes")
    if prior.paper_start_ts_utc != paper_start_ts_utc.isoformat():
        mismatches.append("paper_start_ts_utc")
    if prior.starting_cash_krw != _dstr(config.starting_cash_krw.value):
        mismatches.append("starting_cash_krw")
    if prior.entry_buffer_bps != _dstr(ENTRY_BUFFER_BPS):
        mismatches.append("entry_buffer_bps")
    if prior.entry_lookback_candles != ENTRY_LOOKBACK_CANDLES:
        mismatches.append("entry_lookback_candles")
    if prior.exit_lookback_candles != EXIT_LOOKBACK_CANDLES:
        mismatches.append("exit_lookback_candles")
    if mismatches:
        raise BreakoutResumeDivergenceError(
            f"resumed breakout paper session diverges on: {mismatches!r} "
            "— refusing to continue an audit trail whose inputs changed"
        )


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _parse_utc(iso: str) -> datetime:
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _view_up_to(dataset: CandleDataset, i: int) -> CandleDataset:
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


def _equity_point(
    candle_open_time_utc: datetime, state: LedgerState, close_price: Money
) -> EquityPoint:
    mtm = state.cash_krw.value + state.position_qty.value * close_price.value
    return EquityPoint(
        candle_open_time_utc=candle_open_time_utc,
        cash_krw=state.cash_krw,
        position_qty=state.position_qty,
        close_price=close_price,
        mark_to_market_equity_krw=Money(mtm),
    )


__all__ = [
    "BreakoutInputContractMismatchError",
    "BreakoutPaperConfig",
    "BreakoutResumeDivergenceError",
    "BreakoutRunnerError",
    "BreakoutSessionResult",
    "BreakoutState",
    "EquityPoint",
    "WARMUP_CANDLE_COUNT",
    "run_breakout_paper_session",
]
