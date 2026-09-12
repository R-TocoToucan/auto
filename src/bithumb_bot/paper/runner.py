"""Bounded forward paper-trading runner — fresh-portfolio forward simulation.

Deliberately NOT a reimplementation of the accounting rules. Every
accounting rule (fees, slippage, tick/step rounding, min-order,
notional cap, protective stop, stopped-out lockout) already lives
inside :func:`bithumb_bot.execution.engine.execute_intent` and
:func:`bithumb_bot.execution.stop.evaluate_protective_stop`. This
module adds:

1. A 1,200-candle warm-up / forward split — the first
   :data:`WARMUP_CANDLE_COUNT` candles seed the SMA; only candles at
   or after ``paper_start_ts_utc`` are ever handed to the fresh-
   portfolio forward loop below.
2. An incomplete-candle refusal — a candle whose close boundary has
   not yet passed relative to ``now_utc`` cannot honestly be treated
   as a completed observation, so the whole invocation refuses.
3. A restart-safe, append-only audit trail (``state.json`` +
   ``fills.jsonl`` + ``signals.jsonl``) that a second invocation over
   the same (or append-extended) dataset resumes from without
   duplicating a single line.
4. Warmup isolation (D-erv fix): the underlying strategy's target
   state at every candle is computed from the FULL history via
   :func:`bithumb_bot.strategy.generate_signals` — the SMA/hysteresis
   rule needs the complete lookback window to be correct. But the
   FORWARD-ONLY transition stream actually consumed by the fresh-
   portfolio ledger loop below is derived by :func:`_forward_only_signals`
   under an explicit ``CASH`` baseline at ``paper_start_ts_utc`` — so a
   warm-up-only transition (e.g. a spike inside the warm-up window that
   would have triggered a LONG entry had trading started earlier)
   produces ZERO portfolio effect on the paper session: the paper
   portfolio always begins the forward window in cash, regardless of
   what the strategy would have signalled during warm-up.

No-look-ahead / engine-reuse rationale
---------------------------------------
The shared chronological backtest engine's own main loop (fires a
pending intent at candle-open, evaluates the active protective stop,
then consumes the candle's close-boundary signal) has no notion of
"start the portfolio fresh at candle N" — adding one would require a
warmup-skip flag on that shared engine, which would violate the
"do not modify :mod:`bithumb_bot.execution` / the shared backtest
runner" constraint. Because the strategy is stateless (targets derive
from the rolling SMA + hysteresis computed over the candle window
itself, not from any strategy-side memory), this module instead
duplicates the engine's own event loop shape here, calling the SAME
unmodified :func:`execute_intent` / :func:`evaluate_protective_stop`
primitives directly, over ONLY the forward-window candles, starting
from an explicit fresh :class:`~bithumb_bot.execution.ledger.LedgerState`.
Fee/slippage/rounding/stop/cap semantics are therefore UNCHANGED — they
live inside the primitives, not in this loop. Each candle's view is
truncated to ``[0..i]`` (see :func:`_view_up_to`) for the same reason
the shared engine truncates it: :func:`evaluate_protective_stop`'s own
"walk expected slots up to the last eligible candle" logic would
otherwise scan into genuinely future data if handed the untruncated
dataset.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex
from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.backtest.runner import _DOMAIN_REFUSALS, BacktestResult
from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import (
    FillReplayDivergenceError,
    ForwardDatasetDivergenceError,
    PaperStateDirError,
)
from bithumb_bot.execution import (
    LedgerEntry,
    LedgerState,
    OrderIntent,
    ProtectiveStop,
    evaluate_protective_stop,
    execute_intent,
)
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.paper.state import PaperState, append_jsonl, load_state, read_jsonl, save_state
from bithumb_bot.strategy import StrategySignal, TargetState, generate_signals

#: Fixed at the production ``price_over_sma`` baseline's warm-up length
#: (``BaselineStrategyConfig.PRODUCTION_WARMUP_CANDLES``). Not read from
#: ``config.strategy.warmup_candles`` — the paper split is a property of
#: THIS runner's contract, independent of whatever lookback a test
#: fixture's strategy config happens to use.
WARMUP_CANDLE_COUNT = 1_200


@dataclass(frozen=True)
class PaperSessionResult:
    """Outcome of one :func:`run_paper_session` invocation.

    ``backtest_result`` is a synthesized
    :class:`~bithumb_bot.backtest.runner.BacktestResult` built from the
    fresh-portfolio forward loop's terminal state (``None`` only when
    the run refused before reaching the loop, e.g. insufficient
    candles or an incomplete final candle) — callers needing final
    cash/position, pending intent, active stop, or lockout state read
    it from there rather than duplicating those fields here.
    """

    paper_start_ts_utc: datetime | None
    warmup_candle_count: int
    forward_candle_count: int
    forward_entries: tuple[LedgerEntry, ...]
    forward_signal_count: int
    new_fills_this_invocation: int
    resumed: bool
    invalid_reason: str | None
    refusal_code: str | None
    backtest_result: BacktestResult | None = None


# ---------------------------------------------------------------------------
# canonical serialization helpers (same shape as `research_backtest.py`'s
# `_serialize_ledger_entry` — duplicated here, not imported, to avoid a
# library-layer -> cli-layer import; see plan design_notes)
# ---------------------------------------------------------------------------


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        result: Any = value
    elif isinstance(value, Decimal):
        result = format(value, "f")
    elif isinstance(value, (Money, Qty)):
        result = format(value.value, "f")
    elif isinstance(value, datetime):
        result = value.isoformat()
    elif isinstance(value, dict):
        result = {str(k): _serialize_value(v) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        result = [_serialize_value(v) for v in value]
    else:
        result = str(value)
    return result


def _serialize_ledger_entry(entry: LedgerEntry) -> dict[str, Any]:
    result: dict[str, Any] = _serialize_value(asdict(entry))
    return result


def _serialize_signal(signal: StrategySignal) -> dict[str, Any]:
    return {
        "source_open_time_utc": signal.source_open_time_utc.isoformat(),
        "signal_ts_utc": signal.signal_ts_utc.isoformat(),
        "target_state": signal.target_state,
    }


def _config_fingerprint(config: BacktestConfig) -> dict[str, Any]:
    """Deterministic, content-only fingerprint of ``config``.

    ``run_paper_session`` receives an already-parsed
    :class:`BacktestConfig`, not the TOML file's raw bytes, so
    ``config_sha256`` in ``state.json`` is a hash of THIS fingerprint
    rather than of on-disk file bytes. Content-based hashing is at
    least as strong a drift detector as byte-hashing the source file
    (it is invariant to insignificant TOML formatting changes) and is
    the only option available at this function's signature.
    """
    return {
        "starting_cash_krw": format(config.starting_cash_krw.value, "f"),
        "target_sleeve_fraction": format(config.target_sleeve_fraction, "f"),
        "protective_stop_fraction": format(config.protective_stop_fraction, "f"),
        "strategy": {
            "rule_id": config.strategy.rule_id,
            "ma_type": config.strategy.ma_type,
            "lookback_candles": config.strategy.lookback_candles,
            "warmup_candles": config.strategy.warmup_candles,
            "unit_minutes": config.strategy.unit_minutes,
            "market": config.strategy.market,
            "hysteresis_bps": format(config.strategy.hysteresis_bps, "f"),
        },
        "execution": {
            "slippage_bps_per_side": format(config.execution.slippage_bps_per_side, "f"),
            "max_notional_krw": format(config.execution.max_notional_krw.value, "f"),
            "allow_provisional_fee_model": config.execution.allow_provisional_fee_model,
            "simulation_quantity_quantum": (
                format(config.execution.simulation_quantity_quantum, "f")
                if config.execution.simulation_quantity_quantum is not None
                else None
            ),
        },
    }


def _refused(
    *,
    paper_start_ts_utc: datetime | None,
    forward_candle_count: int,
    resumed: bool,
    reason: str,
    code: str,
) -> PaperSessionResult:
    return PaperSessionResult(
        paper_start_ts_utc=paper_start_ts_utc,
        warmup_candle_count=WARMUP_CANDLE_COUNT,
        forward_candle_count=forward_candle_count,
        forward_entries=(),
        forward_signal_count=0,
        new_fills_this_invocation=0,
        resumed=resumed,
        invalid_reason=reason,
        refusal_code=code,
        backtest_result=None,
    )


def _parse_missing_utc(iso: str) -> datetime:
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _view_up_to(dataset: CandleDataset, i: int) -> CandleDataset:
    """Truncated dataset view containing candles ``[0..i]`` only.

    Duplicated from the shared backtest engine's own main-loop helper
    (same shape, kept local so this module never imports that engine's
    top-level entry point — see module docstring) so
    :func:`~bithumb_bot.execution.engine.execute_intent` and
    :func:`~bithumb_bot.execution.stop.evaluate_protective_stop` never
    see a candle past index ``i``.
    """
    last_open = dataset.candles[i].open_time_utc
    kept_missing = [
        s for s in dataset.missing_intervals_utc if _parse_missing_utc(s) <= last_open
    ]
    return dataset.model_copy(
        update={"candles": dataset.candles[: i + 1], "missing_intervals_utc": kept_missing}
    )


def _forward_only_signals(
    all_candles: Sequence[Candle],
    all_signals: Sequence[StrategySignal],
    paper_start_idx: int,
    unit_minutes: int,
) -> tuple[StrategySignal, ...]:
    """Derive the forward-only transition stream for a FRESH portfolio.

    ``all_signals`` is the full-history transition stream from
    :func:`~bithumb_bot.strategy.generate_signals` (computed over the
    complete warm-up + forward candle window, because the SMA needs the
    full lookback to be correct). This helper re-derives, for each
    forward candle, the TRUE underlying strategy target — the state the
    strategy would be in at that candle given the full history — and
    re-emits a transition ONLY when that state differs from an explicit
    ``CASH`` baseline established at ``paper_start_idx``: a fresh
    portfolio that has never seen any warm-up-sourced transition. See
    the module docstring and the plan's worked trace for the derivation.
    """
    sorted_signals = sorted(all_signals, key=lambda s: s.source_open_time_utc)
    step = timedelta(minutes=unit_minutes)
    forward: list[StrategySignal] = []
    prev_target: TargetState = "CASH"
    true_target: TargetState = "CASH"
    sig_idx = 0
    n_sigs = len(sorted_signals)
    for i in range(paper_start_idx, len(all_candles)):
        candle = all_candles[i]
        open_time = candle.open_time_utc
        while (
            sig_idx < n_sigs
            and sorted_signals[sig_idx].source_open_time_utc <= open_time
        ):
            true_target = sorted_signals[sig_idx].target_state
            sig_idx += 1
        if true_target != prev_target:
            forward.append(
                StrategySignal(
                    target_state=true_target,
                    signal_ts_utc=open_time + step,
                    source_open_time_utc=open_time,
                    unit_minutes=unit_minutes,
                    close_value=candle.close.value,
                    # Not recomputed from the rolling window — never
                    # consumed downstream (only target_state /
                    # source_open_time_utc / signal_ts_utc are
                    # serialized to signals.jsonl or read by the
                    # forward loop below).
                    sma_value=Decimal("0"),
                    lookback_candles=0,
                )
            )
            prev_target = true_target
    return tuple(forward)


def _assert_tz_aware_utc(dataset: CandleDataset) -> None:
    for candle in dataset.candles:
        offset = candle.open_time_utc.utcoffset()
        if candle.open_time_utc.tzinfo is None or offset != timedelta(0):
            raise ValueError(
                f"candle.open_time_utc {candle.open_time_utc!r} is not "
                "tz-aware UTC (defence-in-depth)"
            )


@dataclass(frozen=True)
class _DivergenceHashes:
    """Content hashes/fingerprints compared across resumed invocations."""

    warmup_slice: list[Candle]
    warmup_sha256: str
    config_sha256: str
    snapshot_sha256: str
    hysteresis_str: str


def _compute_divergence_hashes(
    dataset: CandleDataset, config: BacktestConfig, snapshot: SnapshotV1
) -> _DivergenceHashes:
    warmup_slice = dataset.candles[:WARMUP_CANDLE_COUNT]
    return _DivergenceHashes(
        warmup_slice=warmup_slice,
        warmup_sha256=sha256_hex(
            canonical_bytes([c.model_dump(mode="json") for c in warmup_slice])
        ),
        config_sha256=sha256_hex(canonical_bytes(_config_fingerprint(config))),
        snapshot_sha256=sha256_hex(canonical_bytes(snapshot.model_dump(mode="json"))),
        hysteresis_str=format(config.strategy.hysteresis_bps, "f"),
    )


def _resume_divergence_check(
    prior_state: PaperState,
    hashes: _DivergenceHashes,
    *,
    paper_start_ts_utc: datetime,
    dataset: CandleDataset,
    state_dir: Path,
) -> None:
    """Raise :class:`~bithumb_bot.errors.ForwardDatasetDivergenceError`
    if ``prior_state`` disagrees with the current invocation's inputs
    on any resume-sensitive field."""
    mismatches = [
        name
        for name, ok in (
            ("warmup_sha256", prior_state.warmup_sha256 == hashes.warmup_sha256),
            (
                "paper_start_ts_utc",
                prior_state.paper_start_ts_utc == paper_start_ts_utc.isoformat(),
            ),
            ("dataset_market", prior_state.dataset_market == dataset.market),
            ("unit_minutes", prior_state.unit_minutes == dataset.unit_minutes),
            ("hysteresis_bps", prior_state.hysteresis_bps == hashes.hysteresis_str),
            ("config_sha256", prior_state.config_sha256 == hashes.config_sha256),
            ("snapshot_sha256", prior_state.snapshot_sha256 == hashes.snapshot_sha256),
        )
        if not ok
    ]
    if mismatches:
        raise ForwardDatasetDivergenceError(
            f"resumed paper session at {state_dir!s} diverges from the "
            f"current invocation on: {mismatches!r} — refusing to "
            "continue an audit trail whose inputs changed"
        )


def _dataset_shape_refusal(
    dataset: CandleDataset, step: timedelta
) -> tuple[str, str] | None:
    """Return ``(reason, code)`` if ``dataset`` fails contiguity or
    reported-missing-interval validation, else ``None``.

    Duplicated from the shared backtest engine's own pre-flight (this
    runner no longer calls that engine's top-level entry point, so the
    same gap/missing-interval detection must live here to keep
    ``InternalCandleGapError`` / ``ReportedMissingIntervalError``
    reachable as refusals, not raised exceptions).
    """
    prev_open = dataset.candles[0].open_time_utc
    for candle in dataset.candles[1:]:
        expected = prev_open + step
        if candle.open_time_utc != expected:
            return (
                f"internal missing candle: gap between {prev_open.isoformat()} "
                f"and {candle.open_time_utc.isoformat()}",
                "InternalCandleGapError",
            )
        prev_open = candle.open_time_utc

    first_open = dataset.candles[0].open_time_utc
    last_open_overall = dataset.candles[-1].open_time_utc
    for iso in dataset.missing_intervals_utc:
        parsed = _parse_missing_utc(iso)
        if first_open <= parsed <= last_open_overall:
            return (
                f"reported missing interval at {parsed.isoformat()} lies "
                "inside the processed candle range",
                "ReportedMissingIntervalError",
            )
    return None


def _buy_fee_preflight(
    snapshot: SnapshotV1, config: BacktestConfig
) -> tuple[bool, Decimal, None, None] | tuple[None, None, str, str]:
    """Return ``(used_provisional, fee_rate, None, None)`` on success or
    ``(None, None, reason, code)`` on refusal.

    Duplicated from the shared backtest engine's own pre-flight (see
    module docstring: that engine's top-level entry point is no longer
    called here, so its necessary safety check must live here too).
    """
    buy_status = snapshot.verification_status.get("market_buy_fee_reservation")
    if buy_status == "confirmed_read_only":
        return False, snapshot.fee_rates.bid, None, None
    if buy_status == "provisional_documented":
        if not config.execution.allow_provisional_fee_model:
            return (
                None,
                None,
                "snapshot.verification_status['market_buy_fee_reservation'] "
                "== 'provisional_documented' but ExecutionConfig."
                "allow_provisional_fee_model is False — refusing to size a "
                "BUY intent under unverified fee semantics",
                "UnverifiedFeeModelError",
            )
        return True, snapshot.fee_rates.bid, None, None
    return (
        None,
        None,
        "snapshot.verification_status['market_buy_fee_reservation'] == "
        f"{buy_status!r} — not a usable buy-fee model status",
        "UnverifiedFeeModelError",
    )


@dataclass(frozen=True)
class _ForwardLoopResult:
    """Outcome of :func:`_run_forward_loop` — a fresh-portfolio ledger
    walk over ONLY the forward candles (see module docstring)."""

    state: LedgerState
    pending_intent: OrderIntent | None
    active_stop: ProtectiveStop | None
    stopped_out_lockout: bool
    processed_last: datetime | None
    invalid_reason: str | None
    refusal_code: str | None


def _run_forward_loop(
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    config: BacktestConfig,
    forward_signals_by_source: dict[datetime, TargetState],
    fee_rate: Decimal,
) -> _ForwardLoopResult:
    """Fresh-portfolio forward loop — mirrors the shared backtest
    engine's own event order (execute pending intent at open ->
    evaluate stop -> consume close-boundary signal), but starts a
    brand-new :class:`LedgerState` and walks ONLY the forward candles,
    driven by the forward-only signal stream (see module docstring).
    """
    state = LedgerState(cash_krw=config.starting_cash_krw, position_qty=Qty(Decimal("0")))
    pending_intent: OrderIntent | None = None
    active_stop: ProtectiveStop | None = None
    stopped_out_lockout = False
    invalid_reason: str | None = None
    refusal_code: str | None = None
    processed_last: datetime | None = None

    for i in range(WARMUP_CANDLE_COUNT, len(dataset.candles)):
        candle = dataset.candles[i]
        view = _view_up_to(dataset, i)

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

        target = forward_signals_by_source.get(candle.open_time_utc)
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

    return _ForwardLoopResult(
        state=state,
        pending_intent=pending_intent,
        active_stop=active_stop,
        stopped_out_lockout=stopped_out_lockout,
        processed_last=processed_last,
        invalid_reason=invalid_reason,
        refusal_code=refusal_code,
    )


def run_paper_session(
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    config: BacktestConfig,
    state_dir: Path,
    *,
    now_utc: datetime,
) -> PaperSessionResult:
    """Run one bounded forward paper-trading invocation.

    Fail-closed order:

    1. ``state_dir`` must not already exist as a non-directory file
       (:class:`~bithumb_bot.errors.PaperStateDirError`).
    2. The dataset must carry at least ``WARMUP_CANDLE_COUNT + 1``
       candles (refusal via the returned result, not an exception).
    3. Every candle's ``open_time_utc`` must be tz-aware UTC.
    4. The candle sequence must be internally contiguous and must not
       report a missing interval inside the processed range (refusal
       via the returned result — this runner no longer delegates to
       the shared backtest engine's own top-level entry point, so the
       same gap/missing-interval detection lives here now).
    5. Every candle's close boundary must be ``<= now_utc`` — an
       "incomplete" final candle refuses the same way.
    6. If a prior ``state.json`` exists, every resume precondition
       (warm-up hash, ``paper_start_ts_utc``, market, unit, hysteresis
       band, config/snapshot content hashes) must match exactly, else
       :class:`~bithumb_bot.errors.ForwardDatasetDivergenceError`.
    7. The buy-fee model's verification status must be usable.
    8. The fresh-portfolio forward loop runs over ONLY the forward
       candles, starting from an explicit ``CASH`` baseline (see
       module docstring). A domain refusal there propagates via the
       returned result — nothing is written.
    9. The previously-recorded prefix of forward entries/signals MUST
       replay byte-for-byte from the freshly computed forward
       entries/signals, else
       :class:`~bithumb_bot.errors.FillReplayDivergenceError`.
    10. Only the NEW entries/signals beyond that prefix are appended;
        ``state.json`` is atomically replaced.

    This function never reads any environment variable and never
    imports :mod:`bithumb_bot.broker` — it runs with zero credentials
    by construction (nothing in this module's import graph or body
    touches the credential-loading surface).
    """
    if state_dir.exists() and not state_dir.is_dir():
        raise PaperStateDirError(
            f"--state-dir {state_dir!s} exists and is not a directory"
        )
    state_dir.mkdir(parents=True, exist_ok=True)

    if len(dataset.candles) < WARMUP_CANDLE_COUNT + 1:
        return _refused(
            paper_start_ts_utc=None,
            forward_candle_count=0,
            resumed=False,
            reason=(
                f"dataset has {len(dataset.candles)} candles; at least "
                f"{WARMUP_CANDLE_COUNT + 1} required "
                f"({WARMUP_CANDLE_COUNT} warm-up + >=1 forward)"
            ),
            code="InsufficientForwardCandlesError",
        )

    step = timedelta(minutes=dataset.unit_minutes)
    _assert_tz_aware_utc(dataset)

    shape_refusal = _dataset_shape_refusal(dataset, step)
    if shape_refusal is not None:
        reason, code = shape_refusal
        return _refused(
            paper_start_ts_utc=None,
            forward_candle_count=0,
            resumed=False,
            reason=reason,
            code=code,
        )

    paper_start_ts_utc = dataset.candles[WARMUP_CANDLE_COUNT].open_time_utc
    forward_candle_count = len(dataset.candles) - WARMUP_CANDLE_COUNT

    for candle in dataset.candles:
        close_boundary = candle.open_time_utc + step
        if close_boundary > now_utc:
            return _refused(
                paper_start_ts_utc=paper_start_ts_utc,
                forward_candle_count=forward_candle_count,
                resumed=False,
                reason=(
                    f"candle at {candle.open_time_utc.isoformat()} has not "
                    f"closed yet (close boundary {close_boundary.isoformat()} "
                    f"> now_utc {now_utc.isoformat()}) — refusing to treat an "
                    "in-progress candle as a completed observation"
                ),
                code="IncompleteCandleError",
            )

    hashes = _compute_divergence_hashes(dataset, config, snapshot)

    prior_state = load_state(state_dir)
    resumed = prior_state is not None
    prior_fill_count = 0
    prior_signal_count = 0
    if prior_state is not None:
        _resume_divergence_check(
            prior_state,
            hashes,
            paper_start_ts_utc=paper_start_ts_utc,
            dataset=dataset,
            state_dir=state_dir,
        )
        prior_fill_count = prior_state.forward_fill_count
        prior_signal_count = prior_state.forward_signal_count

    used_provisional, fee_rate, fee_reason, fee_code = _buy_fee_preflight(snapshot, config)
    if fee_reason is not None:
        return _refused(
            paper_start_ts_utc=paper_start_ts_utc,
            forward_candle_count=forward_candle_count,
            resumed=resumed,
            reason=fee_reason,
            code=fee_code or "UnverifiedFeeModelError",
        )
    # Narrowed by construction: `_buy_fee_preflight` only returns a
    # `None` reason/code alongside non-`None` (used_provisional, fee_rate).
    assert used_provisional is not None
    assert fee_rate is not None

    all_signals = generate_signals(dataset.candles, config.strategy)
    forward_signals = _forward_only_signals(
        dataset.candles, all_signals, WARMUP_CANDLE_COUNT, dataset.unit_minutes
    )
    forward_signals_by_source: dict[datetime, TargetState] = {
        s.source_open_time_utc: s.target_state for s in forward_signals
    }

    loop_result = _run_forward_loop(
        dataset, snapshot, config, forward_signals_by_source, fee_rate
    )
    state = loop_result.state

    if loop_result.invalid_reason is not None:
        return PaperSessionResult(
            paper_start_ts_utc=paper_start_ts_utc,
            warmup_candle_count=WARMUP_CANDLE_COUNT,
            forward_candle_count=forward_candle_count,
            forward_entries=(),
            forward_signal_count=0,
            new_fills_this_invocation=0,
            resumed=resumed,
            invalid_reason=loop_result.invalid_reason,
            refusal_code=loop_result.refusal_code,
            backtest_result=BacktestResult(
                final_state=state,
                entries=state.entries,
                final_cash_krw=state.cash_krw,
                final_position_qty=state.position_qty,
                active_protective_stop=loop_result.active_stop,
                pending_intent=loop_result.pending_intent,
                stopped_out_lockout=loop_result.stopped_out_lockout,
                processed_first_open_utc=(
                    paper_start_ts_utc if loop_result.processed_last is not None else None
                ),
                processed_last_open_utc=loop_result.processed_last,
                invalid_reason=loop_result.invalid_reason,
                refusal_code=loop_result.refusal_code,
                used_provisional_fee_model=used_provisional,
                signals_generated=len(forward_signals),
            ),
        )

    forward_entries = state.entries

    fills_path = state_dir / "fills.jsonl"
    on_disk_fills = read_jsonl(fills_path)
    recomputed_prior_fills = [
        _serialize_ledger_entry(e) for e in forward_entries[:prior_fill_count]
    ]
    _assert_prefix_replay(recomputed_prior_fills, on_disk_fills, fills_path)
    new_entries = forward_entries[prior_fill_count:]

    signals_path = state_dir / "signals.jsonl"
    on_disk_signals = read_jsonl(signals_path)
    recomputed_prior_signals = [
        _serialize_signal(s) for s in forward_signals[:prior_signal_count]
    ]
    _assert_prefix_replay(recomputed_prior_signals, on_disk_signals, signals_path)
    new_signals = forward_signals[prior_signal_count:]

    for entry in new_entries:
        append_jsonl(fills_path, _serialize_ledger_entry(entry))
    for sig in new_signals:
        append_jsonl(signals_path, _serialize_signal(sig))

    new_state = PaperState(
        schema_version=2,
        run_purpose="engineering_smoke",
        selection_eligible=False,
        holdout_eligible=False,
        hysteresis_bps=hashes.hysteresis_str,
        paper_start_ts_utc=paper_start_ts_utc.isoformat(),
        paper_start_cash_krw=format(config.starting_cash_krw.value, "f"),
        paper_start_position_qty="0",
        paper_start_realized_pnl_krw="0",
        paper_start_cumulative_fees_krw="0",
        warmup_first_open_utc=hashes.warmup_slice[0].open_time_utc.isoformat(),
        warmup_last_open_utc=hashes.warmup_slice[-1].open_time_utc.isoformat(),
        warmup_candles=WARMUP_CANDLE_COUNT,
        warmup_sha256=hashes.warmup_sha256,
        config_sha256=hashes.config_sha256,
        snapshot_sha256=hashes.snapshot_sha256,
        dataset_market=dataset.market,
        unit_minutes=dataset.unit_minutes,
        last_processed_open_utc=dataset.candles[-1].open_time_utc.isoformat(),
        forward_candle_count=forward_candle_count,
        forward_signal_count=len(forward_signals),
        forward_fill_count=len(forward_entries),
        final_cash_krw=format(state.cash_krw.value, "f"),
        final_position_qty=format(state.position_qty.value, "f"),
    )
    save_state(state_dir, new_state)

    return PaperSessionResult(
        paper_start_ts_utc=paper_start_ts_utc,
        warmup_candle_count=WARMUP_CANDLE_COUNT,
        forward_candle_count=forward_candle_count,
        forward_entries=forward_entries,
        forward_signal_count=len(forward_signals),
        new_fills_this_invocation=len(new_entries),
        resumed=resumed,
        invalid_reason=None,
        refusal_code=None,
        backtest_result=BacktestResult(
            final_state=state,
            entries=forward_entries,
            final_cash_krw=state.cash_krw,
            final_position_qty=state.position_qty,
            active_protective_stop=loop_result.active_stop,
            pending_intent=loop_result.pending_intent,
            stopped_out_lockout=loop_result.stopped_out_lockout,
            processed_first_open_utc=(
                paper_start_ts_utc if loop_result.processed_last is not None else None
            ),
            processed_last_open_utc=loop_result.processed_last,
            invalid_reason=None,
            refusal_code=None,
            used_provisional_fee_model=used_provisional,
            signals_generated=len(forward_signals),
        ),
    )


def _assert_prefix_replay(
    recomputed: list[dict[str, Any]], on_disk: list[dict[str, Any]], path: Path
) -> None:
    if recomputed != on_disk:
        raise FillReplayDivergenceError(
            f"replaying the engine/strategy over the recorded forward "
            f"window did NOT reproduce {path!s} byte-for-byte — refusing "
            "to extend a possibly-corrupted audit trail"
        )


__all__ = ["WARMUP_CANDLE_COUNT", "PaperSessionResult", "run_paper_session"]
