"""Bounded forward paper-trading runner — fresh-portfolio forward simulation.

Deliberately NOT a reimplementation of the accounting rules. Every
accounting rule (fees, slippage, tick/step rounding, min-order,
notional cap, protective stop, stopped-out lockout, contiguity/gap
detection, tz-awareness, the buy-fee model's verification status) is
enforced inside the single call to
:func:`bithumb_bot.backtest.runner.run_backtest` this module makes on
a reduced dataset (see "Warmup isolation rationale" below). This
module adds:

1. A 1,200-candle warm-up / forward split — the first
   :data:`WARMUP_CANDLE_COUNT` candles seed the SMA; only candles at
   or after ``paper_start_ts_utc`` are ever treated as forward-window
   results.
2. An incomplete-candle refusal — a candle whose close boundary has
   not yet passed relative to ``now_utc`` cannot honestly be treated
   as a completed observation, so the whole invocation refuses.
3. A restart-safe, append-only audit trail (``state.json`` +
   ``fills.jsonl`` + ``signals.jsonl`` + ``candle_fingerprints.jsonl``)
   that a second invocation over the same (or append-extended) dataset
   resumes from without duplicating a single line.
4. Warmup isolation (D-no0 D1 fix): the paper runner builds a REDUCED
   dataset containing the last ``lookback - 1`` pre-paper candles plus
   all forward candles, and calls the UNMODIFIED
   :func:`bithumb_bot.backtest.runner.run_backtest` on it exactly
   once. The first candle in the reduced dataset for which
   :func:`~bithumb_bot.strategy.generate_signals` can compute an SMA
   is ``paper_start_ts_utc`` itself; by construction the strategy's
   internal ``current_state`` is still ``CASH`` at that point, so no
   warmup-sourced transition can affect the forward window.
5. Immutable-prefix verification (D-erv D2 fix, tightened by D-no0):
   every processed forward candle's SHA-256 fingerprint is persisted
   to ``candle_fingerprints.jsonl`` (see :func:`~bithumb_bot.paper.
   state.candle_fingerprint`). On resume, BEFORE any new line is
   appended anywhere and BEFORE the single ``run_backtest`` call, every
   recorded fingerprint is re-verified against the current dataset —
   a mismatch (the candle's bytes changed), a missing candle, a
   row-count disagreement, a reordered row, a tail disagreement, or a
   malformed row all raise
   :class:`~bithumb_bot.errors.ProcessedPrefixMutatedError`. This is
   STRICTER than the existing fills/signals prefix-replay check: a
   mutated candle whose DERIVED signal/fill happens to coincidentally
   still match would otherwise slip past that check.

Warmup isolation rationale
---------------------------
The ``price_over_sma`` strategy is stateful:
:func:`~bithumb_bot.strategy.generate_signals` keeps an internal
``current_state: TargetState`` variable across candles in its loop
and only emits a signal on a transition from that state (see
``bithumb_bot/strategy/baseline.py``). Feeding warmup candles into
``generate_signals`` writes that state; if the paper runner then
called ``generate_signals`` over the full warmup+forward window, the
forward slice would inherit whatever state the warmup produced — a
leak. The runner avoids this by constructing a REDUCED dataset (last
``lookback - 1`` pre-paper candles + all forward candles) and calling
the UNMODIFIED ``run_backtest`` on it exactly once: the first candle
in the reduced dataset with a valid SMA is ``paper_start_ts_utc``
itself, and ``generate_signals`` sees no earlier candle that could
have transitioned it out of the initial CASH state. No
:mod:`bithumb_bot.strategy` code changes, no
:mod:`bithumb_bot.backtest` code changes, no
:mod:`bithumb_bot.execution` code changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex
from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.backtest.runner import BacktestResult, run_backtest
from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import (
    FillReplayDivergenceError,
    ForwardDatasetDivergenceError,
    PaperStateDirError,
    ProcessedPrefixMutatedError,
)
from bithumb_bot.execution import LedgerEntry
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.paper.state import (
    PaperState,
    append_fingerprint,
    append_jsonl,
    candle_fingerprint,
    load_state,
    read_fingerprints,
    read_jsonl,
    save_state,
)
from bithumb_bot.strategy import StrategySignal, generate_signals

#: Fixed at the production ``price_over_sma`` baseline's warm-up length
#: (``BaselineStrategyConfig.PRODUCTION_WARMUP_CANDLES``). Not read from
#: ``config.strategy.warmup_candles`` — the paper split is a property of
#: THIS runner's contract, independent of whatever lookback a test
#: fixture's strategy config happens to use.
WARMUP_CANDLE_COUNT = 1_200


@dataclass(frozen=True)
class PaperSessionResult:
    """Outcome of one :func:`run_paper_session` invocation.

    ``backtest_result`` is the :class:`~bithumb_bot.backtest.runner.
    BacktestResult` returned by the single ``run_backtest`` call over
    the reduced dataset (``None`` only when the run refused before
    reaching that call, e.g. insufficient candles, insufficient
    lookback, or an incomplete final candle) — callers needing final
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


def _build_reduced_dataset(dataset: CandleDataset, reduce_start_idx: int) -> CandleDataset:
    """Return a REDUCED :class:`CandleDataset` for the single
    ``run_backtest`` call (D1 fix): the last ``lookback - 1`` pre-paper
    candles plus every forward candle, so the strategy's internal
    ``current_state`` starts fresh at ``paper_start_ts_utc`` (see module
    docstring).

    Mirrors the dataset-copy technique the shared backtest engine's own
    truncated-view helper uses, so the schema round-trip through
    ``model_copy`` is preserved.
    """
    reduced_candles = dataset.candles[reduce_start_idx:]
    kept_missing = [
        s
        for s in dataset.missing_intervals_utc
        if _parse_missing_utc(s) >= reduced_candles[0].open_time_utc
    ]
    return dataset.model_copy(
        update={"candles": reduced_candles, "missing_intervals_utc": kept_missing}
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


def _verify_processed_prefix_fingerprints(
    state_dir: Path, dataset: CandleDataset, prior_state: PaperState
) -> None:
    """Raise :class:`~bithumb_bot.errors.ProcessedPrefixMutatedError` if any
    previously processed forward candle's recorded SHA-256 fingerprint
    (``candle_fingerprints.jsonl``) no longer matches the current dataset,
    or if a previously processed candle is missing from it entirely.

    Runs AFTER :func:`_resume_divergence_check` (an invocation-level input
    drift — warmup slice hash, config, snapshot, market, unit, hysteresis —
    surfaces as :class:`~bithumb_bot.errors.ForwardDatasetDivergenceError`
    first, preserving already-tested behaviour) and BEFORE
    :func:`_assert_prefix_replay` / the single ``run_backtest`` call — a
    candle-content mutation whose DERIVED signal/fill happens to
    coincidentally still match would otherwise slip past that looser
    fills/signals replay check.
    """
    recorded = read_fingerprints(state_dir)
    if not recorded and prior_state.forward_candle_count > 0:
        raise ProcessedPrefixMutatedError(
            "prior state.json reports forward_candle_count="
            f"{prior_state.forward_candle_count} but candle_fingerprints.jsonl "
            "is empty — audit trail incomplete"
        )
    current_by_open_time = {c.open_time_utc: c for c in dataset.candles}
    for row in recorded:
        open_time = _parse_missing_utc(row["open_time_utc"])
        current = current_by_open_time.get(open_time)
        if current is None:
            raise ProcessedPrefixMutatedError(
                f"previously processed forward candle at {open_time.isoformat()} "
                "is missing from the current dataset — refusing to append "
                "onto a candle prefix whose content changed"
            )
        if candle_fingerprint(current) != row["sha256"]:
            raise ProcessedPrefixMutatedError(
                f"previously processed forward candle at {open_time.isoformat()} "
                "has mutated (SHA-256 mismatch) — refusing to append onto a "
                "candle prefix whose content changed"
            )


@dataclass(frozen=True)
class _ResumeContext:
    """Prior-invocation cursor positions a resumed run appends onto."""

    resumed: bool
    prior_fill_count: int
    prior_signal_count: int
    prior_forward_candle_count: int


def _load_resume_context(
    state_dir: Path,
    dataset: CandleDataset,
    hashes: _DivergenceHashes,
    paper_start_ts_utc: datetime,
) -> _ResumeContext:
    """Load prior ``state.json`` (if any) and run every resume-time
    guard — invocation-level divergence first, then the D2
    candle-content fingerprint verification — before returning the
    prior audit-trail cursor positions the caller appends onto."""
    prior_state = load_state(state_dir)
    if prior_state is None:
        return _ResumeContext(
            resumed=False,
            prior_fill_count=0,
            prior_signal_count=0,
            prior_forward_candle_count=0,
        )
    _resume_divergence_check(
        prior_state,
        hashes,
        paper_start_ts_utc=paper_start_ts_utc,
        dataset=dataset,
        state_dir=state_dir,
    )
    _verify_processed_prefix_fingerprints(state_dir, dataset, prior_state)
    return _ResumeContext(
        resumed=True,
        prior_fill_count=prior_state.forward_fill_count,
        prior_signal_count=prior_state.forward_signal_count,
        prior_forward_candle_count=prior_state.forward_candle_count,
    )


def _append_new_fingerprints(
    state_dir: Path, dataset: CandleDataset, prior_forward_candle_count: int
) -> None:
    """Append one fingerprint per NEW forward candle processed this
    invocation, BEFORE any corresponding fill/signal line — fingerprint-
    first sequencing means a crash mid-write leaves the fingerprint log
    strictly <= the fills/signals log, which is fail-closed on the next
    resume's :func:`_verify_processed_prefix_fingerprints`."""
    new_forward_candle_start = WARMUP_CANDLE_COUNT + prior_forward_candle_count
    for idx in range(new_forward_candle_start, len(dataset.candles)):
        append_fingerprint(state_dir, dataset.candles[idx])


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
    3. ``config.strategy.lookback_candles`` must not require more
       pre-paper candles than ``WARMUP_CANDLE_COUNT`` provides
       (refusal via the returned result, code
       ``InsufficientPaperLookbackError``).
    4. Every candle's close boundary must be ``<= now_utc`` — an
       "incomplete" final candle refuses the same way.
    5. If a prior ``state.json`` exists, every resume precondition
       (warm-up hash, ``paper_start_ts_utc``, market, unit, hysteresis
       band, config/snapshot content hashes) must match exactly, else
       :class:`~bithumb_bot.errors.ForwardDatasetDivergenceError`; then
       every previously processed forward candle's recorded fingerprint
       is re-verified against the current dataset, else
       :class:`~bithumb_bot.errors.ProcessedPrefixMutatedError`.
    6. A REDUCED dataset (the last ``lookback - 1`` pre-paper candles
       plus every forward candle) is built and handed to the
       UNMODIFIED :func:`~bithumb_bot.backtest.runner.run_backtest`
       exactly once (see module docstring). Every accounting rule —
       contiguity, gap detection, tz-awareness, the buy-fee model's
       verification status, fees, slippage, tick/step rounding,
       min-order, notional cap, protective stop, stopped-out lockout —
       is enforced inside that single call; a domain refusal there
       propagates via the returned result, nothing is written.
    7. The previously-recorded prefix of forward entries/signals MUST
       replay byte-for-byte from the freshly computed forward
       entries/signals, else
       :class:`~bithumb_bot.errors.FillReplayDivergenceError`.
    8. Only the NEW entries/signals beyond that prefix are appended;
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

    lookback = config.strategy.lookback_candles
    pre_needed = lookback - 1
    reduce_start_idx = WARMUP_CANDLE_COUNT - pre_needed
    if reduce_start_idx < 0:
        return _refused(
            paper_start_ts_utc=None,
            forward_candle_count=0,
            resumed=False,
            reason=(
                f"config.strategy.lookback_candles={lookback} requires "
                f"{pre_needed} pre-paper candles but WARMUP_CANDLE_COUNT="
                f"{WARMUP_CANDLE_COUNT} only provides {WARMUP_CANDLE_COUNT} "
                "— increase warmup or reduce lookback"
            ),
            code="InsufficientPaperLookbackError",
        )

    paper_start_ts_utc = dataset.candles[WARMUP_CANDLE_COUNT].open_time_utc
    forward_candle_count = len(dataset.candles) - WARMUP_CANDLE_COUNT
    step = timedelta(minutes=dataset.unit_minutes)

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
    resume_ctx = _load_resume_context(state_dir, dataset, hashes, paper_start_ts_utc)
    resumed = resume_ctx.resumed
    prior_fill_count = resume_ctx.prior_fill_count
    prior_signal_count = resume_ctx.prior_signal_count
    prior_forward_candle_count = resume_ctx.prior_forward_candle_count

    reduced_dataset = _build_reduced_dataset(dataset, reduce_start_idx)
    br = run_backtest(reduced_dataset, snapshot, config)

    if br.invalid_reason is not None:
        return PaperSessionResult(
            paper_start_ts_utc=paper_start_ts_utc,
            warmup_candle_count=WARMUP_CANDLE_COUNT,
            forward_candle_count=forward_candle_count,
            forward_entries=(),
            forward_signal_count=0,
            new_fills_this_invocation=0,
            resumed=resumed,
            invalid_reason=br.invalid_reason,
            refusal_code=br.refusal_code,
            backtest_result=br,
        )

    # Belt-and-suspenders filter: by construction every entry `run_backtest`
    # produces over `reduced_dataset` is already forward-window (no signal
    # can fire before the reduced dataset's last `lookback` candles are
    # seen), but this filter is a cheap, defensive guard against any future
    # change to the strategy's warm-up rule.
    forward_entries = tuple(
        e for e in br.entries if e.source_open_time_utc >= paper_start_ts_utc
    )
    forward_signals = tuple(
        s
        for s in generate_signals(reduced_dataset.candles, config.strategy)
        if s.source_open_time_utc >= paper_start_ts_utc
    )

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

    # D2 (D-erv, tightened D-no0): fingerprint-first sequencing — see
    # helper docstring.
    _append_new_fingerprints(state_dir, dataset, prior_forward_candle_count)

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
        final_cash_krw=format(br.final_cash_krw.value, "f"),
        final_position_qty=format(br.final_position_qty.value, "f"),
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
        backtest_result=br,
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
