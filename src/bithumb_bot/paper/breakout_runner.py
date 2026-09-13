"""Bounded forward paper-trading runner for the breakout shadow candidate.

Runs alongside the existing SMA-75 paper runner
(:mod:`bithumb_bot.paper.runner`) with a fully separate state
directory, ledger, fills log, signals log, and equity trace. Never
imports :mod:`bithumb_bot.broker`; never calls a live-order endpoint.

Resume model — **deterministic full replay** (corrects the seed-from-
final-cash approach in 7ca7f78). On every invocation the runner:

1. Runs input-contract + incomplete-final-candle refusals.
2. Loads the prior :class:`BreakoutState` if present, and verifies
   invocation-level divergence (market, unit, cash, paper-start,
   fixed strategy params).
3. Reads the persisted prefix logs (``candle_fingerprints.jsonl``,
   ``fills.jsonl``, ``signals.jsonl``, ``equity.jsonl``).
4. Verifies every previously processed forward candle against the
   current dataset via :func:`bithumb_bot.paper.state.candle_fingerprint`
   (missing / reordered / shortened / mutated → fail closed with
   :class:`~bithumb_bot.errors.ProcessedPrefixMutatedError`).
5. Replays the WHOLE forward window from ``paper_start_ts_utc`` using
   the operator-supplied ``starting_cash_krw`` — never seeded from a
   stored ``final_cash_krw``. LONG state, an in-flight pending BUY /
   SELL, and a live ``stopped_out_lockout`` all emerge naturally from
   replay.
6. Compares the replay's fills / signals / equity against the
   persisted prefix; any divergence fails closed
   (:class:`~bithumb_bot.errors.ProcessedPrefixMutatedError`).
7. If every check and the replay itself succeed, appends only the
   NEW suffix of each log and overwrites ``state.json``.

If ANY of steps 1-6 refuses — or ``execute_intent`` raises during
step 5's replay — nothing on disk is mutated. State files are the
sole source of truth about "what was processed"; replay is the sole
source of truth about "what the ledger + equity look like now".

Equity is recorded once per completed forward candle (mark-to-market
at that candle's ``close``, using the state AFTER any fills produced
on that candle), never per fill.

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

from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex, write_with_sidecar
from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import (
    BelowMinimumOrderError,
    InsufficientCashError,
    InsufficientPositionError,
    MissingIntervalInStopWindowError,
    NoNextCandleError,
    NotionalCapExceededError,
    ProcessedPrefixMutatedError,
    SidecarHashMismatchError,
    SnapshotValidationError,
    UnverifiedFeeModelError,
)
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.execution.engine import execute_intent
from bithumb_bot.execution.intent import OrderIntent
from bithumb_bot.execution.ledger import LedgerEntry, LedgerState
from bithumb_bot.execution.stop import ProtectiveStop, evaluate_protective_stop
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.paper.state import (
    append_jsonl,
    candle_fingerprint,
    read_jsonl,
)
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
#: evaluable candle in the full dataset is exactly ``paper_start_ts_utc``.
WARMUP_CANDLE_COUNT: int = ENTRY_LOOKBACK_CANDLES

_PROTECTIVE_STOP_FRACTION: Decimal = Decimal("0.10")
_TARGET_SLEEVE_FRACTION: Decimal = Decimal("1.0")
#: Bumped in the 7ca7f78-followup correction: schema_version=2 marks
#: the deterministic-full-replay resume model (fields are the same
#: shape; the fields' semantics changed).
_SCHEMA_VERSION: int = 2
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


# ---------------------------------------------------------------------------
# public dataclasses
# ---------------------------------------------------------------------------


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
    """Mark-to-market equity snapshot at a forward-candle boundary."""

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
    new_equity_points_this_invocation: int
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
# state persistence (state.json shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakoutState:
    """Byte-exact ``state.json`` shape. Every Decimal stored as a string."""

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
    #: Informational only — the source of truth is deterministic replay.
    final_cash_krw: str
    final_position_qty: str
    stopped_out_lockout: bool
    active_stop_price: str | None
    entry_breakout_level: str | None
    last_processed_open_utc: str | None
    forward_fill_count: int
    forward_signal_count: int
    forward_candle_count: int
    forward_equity_count: int


# ---------------------------------------------------------------------------
# error types
# ---------------------------------------------------------------------------


class BreakoutRunnerError(Exception):
    """Base class for shadow-runner refusals surfaced to the CLI."""


class BreakoutInputContractMismatchError(BreakoutRunnerError):
    """Dataset market or unit_minutes disagrees with the breakout candidate."""


class BreakoutResumeDivergenceError(BreakoutRunnerError):
    """Resumed state.json disagrees with the current invocation's inputs."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _dstr(d: Decimal) -> str:
    return format(d, "f")


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


# ---------------------------------------------------------------------------
# serialization
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

    result: dict[str, Any] = _conv(asdict(entry))
    return result


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


def _fingerprint_row(index: int) -> str:
    """Canonical file path label used in error messages."""
    return f"candle_fingerprints.jsonl row {index}"


# ---------------------------------------------------------------------------
# state.json + sidecar I/O (uses artifact.canonical for sha256 sidecar)
# ---------------------------------------------------------------------------


def _load_state(state_dir: Path) -> BreakoutState | None:
    path = state_dir / "state.json"
    if not path.is_file():
        return None
    data = path.read_bytes()
    on_disk_hex = sha256_hex(data)
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise SidecarHashMismatchError(path)
    recorded = sidecar.read_text(encoding="utf-8").strip().split()
    if len(recorded) < 1 or len(recorded[0]) != 64 or recorded[0] != on_disk_hex:
        raise SidecarHashMismatchError(path)
    parsed = json.loads(data.decode("utf-8"))
    on_disk_schema = parsed.get("schema_version") if isinstance(parsed, dict) else None
    if on_disk_schema != _SCHEMA_VERSION:
        raise BreakoutResumeDivergenceError(
            f"state.json schema_version={on_disk_schema!r} does not match "
            f"current runner's schema_version={_SCHEMA_VERSION} — delete "
            "state.json to start a fresh session"
        )
    return BreakoutState(**parsed)


def _save_state(state_dir: Path, state: BreakoutState) -> None:
    target = state_dir / "state.json"
    write_with_sidecar(target, canonical_bytes(asdict(state)))


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
        new_equity_points_this_invocation=0,
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
# divergence / prefix-integrity checks
# ---------------------------------------------------------------------------


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


def _verify_prefix_fingerprints(
    persisted_fingerprints: list[dict[str, Any]],
    dataset: CandleDataset,
    prior: BreakoutState | None,
    fingerprints_path: Path,
) -> None:
    """Fail closed on any deviation between the persisted fingerprint
    prefix and the current dataset's forward candles at the SAME
    position. Runs BEFORE any file mutation on the resumed invocation.

    Checks (in this exact order, first failure wins):

    1. missing-file — ``candle_fingerprints.jsonl`` absent while the
       prior state reports processed forward candles.
    2. malformed row — a stored row missing / malformed
       ``open_time_utc`` or ``sha256``.
    3. row count — persisted row count disagrees with the prior state's
       ``forward_candle_count``.
    4. per-row order + content — recorded row ``i`` must match the
       current dataset's forward candle at index ``WARMUP_CANDLE_COUNT
       + i`` on both ``open_time_utc`` and SHA-256 fingerprint.
    5. tail — the last recorded row's ``open_time_utc`` must match the
       prior state's ``last_processed_open_utc``.
    """
    prior_count = prior.forward_candle_count if prior is not None else 0

    if prior_count > 0 and not fingerprints_path.is_file():
        raise ProcessedPrefixMutatedError(
            f"candle_fingerprints.jsonl is missing at {fingerprints_path!s} "
            f"but prior state.json reports forward_candle_count={prior_count} "
            "— audit trail incomplete"
        )

    for i, row in enumerate(persisted_fingerprints):
        if not isinstance(row, dict):
            raise ProcessedPrefixMutatedError(
                f"{_fingerprint_row(i)}: expected JSON object, got "
                f"{type(row).__name__}"
            )
        open_time_utc = row.get("open_time_utc")
        sha256_value = row.get("sha256")
        if not isinstance(open_time_utc, str):
            raise ProcessedPrefixMutatedError(
                f"{_fingerprint_row(i)}: missing / non-string 'open_time_utc'"
            )
        if not isinstance(sha256_value, str) or len(sha256_value) != 64:
            raise ProcessedPrefixMutatedError(
                f"{_fingerprint_row(i)}: missing / malformed 'sha256'"
            )
        try:
            recorded_open = _parse_utc(open_time_utc)
        except ValueError as exc:
            raise ProcessedPrefixMutatedError(
                f"{_fingerprint_row(i)}: 'open_time_utc' is not ISO-8601 "
                f"({exc})"
            ) from exc
        expected_idx = WARMUP_CANDLE_COUNT + i
        if expected_idx >= len(dataset.candles):
            raise ProcessedPrefixMutatedError(
                f"previously processed forward candle at "
                f"{recorded_open.isoformat()} is missing from the current "
                "dataset — refusing to append onto a shortened prefix"
            )
        expected_candle = dataset.candles[expected_idx]
        if recorded_open != expected_candle.open_time_utc:
            raise ProcessedPrefixMutatedError(
                f"{_fingerprint_row(i)}: open_time_utc="
                f"{recorded_open.isoformat()} != current dataset "
                f"candle[{expected_idx}].open_time_utc="
                f"{expected_candle.open_time_utc.isoformat()} — reordered "
                "or dataset diverged"
            )
        if candle_fingerprint(expected_candle) != sha256_value:
            raise ProcessedPrefixMutatedError(
                f"candle at {recorded_open.isoformat()} has mutated "
                "(SHA-256 mismatch) — refusing to append onto a candle "
                "prefix whose content changed"
            )

    if len(persisted_fingerprints) != prior_count:
        raise ProcessedPrefixMutatedError(
            f"candle_fingerprints.jsonl has {len(persisted_fingerprints)} "
            f"rows but state.json reports forward_candle_count={prior_count} "
            "— audit trail truncated, duplicated, or extended"
        )

    if prior is not None and persisted_fingerprints:
        last = persisted_fingerprints[-1]["open_time_utc"]
        if last != prior.last_processed_open_utc:
            raise ProcessedPrefixMutatedError(
                f"candle_fingerprints.jsonl last row open_time_utc={last} "
                f"!= state.json last_processed_open_utc="
                f"{prior.last_processed_open_utc} — tail divergence"
            )


def _verify_prefix_matches_replay(
    name: str,
    persisted: list[dict[str, Any]],
    replayed_serialized: list[dict[str, Any]],
) -> None:
    """Fail closed if ``persisted`` is not a byte-equal prefix of
    ``replayed_serialized``. Only compares up to ``len(persisted)``
    entries — the rest is the new suffix to append.
    """
    if len(persisted) > len(replayed_serialized):
        raise ProcessedPrefixMutatedError(
            f"{name}.jsonl has {len(persisted)} rows but replay produced "
            f"only {len(replayed_serialized)} — persisted prefix longer "
            "than replay (dataset shrank or replay diverged)"
        )
    for i, (p, r) in enumerate(zip(persisted, replayed_serialized)):
        if p != r:
            raise ProcessedPrefixMutatedError(
                f"{name}.jsonl row {i} disagrees with deterministic replay "
                "— audit trail diverged"
            )


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def run_breakout_paper_session(
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    config: BreakoutPaperConfig,
    state_dir: Path,
    *,
    now_utc: datetime,
) -> BreakoutSessionResult:
    """Run one bounded shadow-candidate forward paper session with
    deterministic full replay from ``paper_start_ts_utc``."""
    zero_qty = Qty(Decimal("0"))

    # ---- STAGE 1: input contract + incomplete-candle refusals ----
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
    fingerprints_path = state_dir / "candle_fingerprints.jsonl"

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
    forward_candles_all = dataset.candles[WARMUP_CANDLE_COUNT:]

    for candle in dataset.candles:
        close_boundary = candle.open_time_utc + step
        if close_boundary > now_utc:
            return _refused(
                paper_start_ts_utc=paper_start_ts_utc,
                forward_candle_count=len(forward_candles_all),
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

    # ---- STAGE 2: load prior state + invocation-level divergence check ----
    prior = _load_state(state_dir)
    resumed = prior is not None
    if prior is not None:
        _check_resume_divergence(prior, dataset, config, paper_start_ts_utc)

    # ---- STAGE 3: load persisted prefix logs ----
    persisted_fingerprints = read_jsonl(fingerprints_path)
    persisted_fills = read_jsonl(fills_path)
    persisted_signals = read_jsonl(signals_path)
    persisted_equity = read_jsonl(equity_path)

    # ---- STAGE 4: verify fingerprint prefix against the current dataset
    # (before ANY file mutation and before the potentially expensive replay).
    _verify_prefix_fingerprints(
        persisted_fingerprints, dataset, prior, fingerprints_path
    )

    # ---- STAGE 5: deterministic full replay from paper_start ----
    # Signal generation. Since WARMUP_CANDLE_COUNT == ENTRY_LOOKBACK_CANDLES,
    # feeding the full dataset produces the same result as the reduced-
    # dataset trick used elsewhere — the strategy's first evaluable candle
    # is exactly paper_start.
    all_signals = generate_breakout_signals(dataset.candles)
    signals_by_source: dict[datetime, BreakoutSignal] = {
        s.source_open_time_utc: s for s in all_signals
    }

    state = LedgerState(
        cash_krw=config.starting_cash_krw, position_qty=zero_qty
    )
    pending_intent: OrderIntent | None = None
    active_stop: ProtectiveStop | None = None
    stopped_out_lockout: bool = False
    entry_breakout_level: Decimal | None = None

    replayed_entries: list[LedgerEntry] = []
    replayed_signals: list[BreakoutSignal] = []
    replayed_equity: list[EquityPoint] = []
    replayed_fingerprints: list[dict[str, str]] = []

    invalid_reason: str | None = None
    refusal_code: str | None = None
    last_processed_open: datetime | None = None

    for i in range(WARMUP_CANDLE_COUNT, len(dataset.candles)):
        candle = dataset.candles[i]
        view = _view_up_to(dataset, i)
        replayed_fingerprints.append(
            {
                "open_time_utc": candle.open_time_utc.isoformat(),
                "sha256": candle_fingerprint(candle),
            }
        )

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
            replayed_entries.append(entry)
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
                replayed_entries.append(evaluation.fill_entry)

        # Consume this candle's strategy signal.
        signal = signals_by_source.get(candle.open_time_utc)
        if signal is not None:
            replayed_signals.append(signal)
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

        # One equity point per completed forward candle — always, whether
        # or not a fill happened this candle. Snapshot taken AFTER all
        # events on this candle so it reflects the state at candle close.
        replayed_equity.append(
            _equity_point(candle.open_time_utc, state, candle.close)
        )
        last_processed_open = candle.open_time_utc

    # ---- STAGE 6: refusal short-circuit (no mutation) ----
    if invalid_reason is not None:
        return BreakoutSessionResult(
            paper_start_ts_utc=paper_start_ts_utc,
            warmup_candle_count=WARMUP_CANDLE_COUNT,
            forward_candle_count=len(forward_candles_all),
            forward_entries=(),
            forward_signal_count=0,
            new_fills_this_invocation=0,
            new_signals_this_invocation=0,
            new_equity_points_this_invocation=0,
            resumed=resumed,
            invalid_reason=invalid_reason,
            refusal_code=refusal_code,
            final_cash_krw=state.cash_krw,
            final_position_qty=state.position_qty,
            equity_curve=(),
            stopped_out_lockout=stopped_out_lockout,
            active_stop_price=(
                active_stop.stop_price if active_stop is not None else None
            ),
            entry_breakout_level=entry_breakout_level,
            last_processed_open_utc=last_processed_open,
        )

    # ---- STAGE 7: prefix-vs-replay comparison (no mutation yet) ----
    replayed_fills_serialized = [
        _serialize_ledger_entry(e) for e in replayed_entries
    ]
    replayed_signals_serialized = [
        _serialize_signal(s) for s in replayed_signals
    ]
    replayed_equity_serialized = [
        _serialize_equity_point(p) for p in replayed_equity
    ]

    _verify_prefix_matches_replay("fills", persisted_fills, replayed_fills_serialized)
    _verify_prefix_matches_replay(
        "signals", persisted_signals, replayed_signals_serialized
    )
    _verify_prefix_matches_replay(
        "equity", persisted_equity, replayed_equity_serialized
    )

    # ---- STAGE 8: append new suffix + save state (mutation) ----
    for row in replayed_fingerprints[len(persisted_fingerprints):]:
        append_jsonl(fingerprints_path, row)
    for row in replayed_fills_serialized[len(persisted_fills):]:
        append_jsonl(fills_path, row)
    for row in replayed_signals_serialized[len(persisted_signals):]:
        append_jsonl(signals_path, row)
    for row in replayed_equity_serialized[len(persisted_equity):]:
        append_jsonl(equity_path, row)

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
        last_processed_open_utc=(
            last_processed_open.isoformat()
            if last_processed_open is not None
            else None
        ),
        forward_fill_count=len(replayed_entries),
        forward_signal_count=len(replayed_signals),
        forward_candle_count=len(replayed_equity),
        forward_equity_count=len(replayed_equity),
    )
    _save_state(state_dir, new_state)

    return BreakoutSessionResult(
        paper_start_ts_utc=paper_start_ts_utc,
        warmup_candle_count=WARMUP_CANDLE_COUNT,
        forward_candle_count=len(replayed_equity),
        forward_entries=tuple(replayed_entries),
        forward_signal_count=len(replayed_signals),
        new_fills_this_invocation=(
            len(replayed_entries) - len(persisted_fills)
        ),
        new_signals_this_invocation=(
            len(replayed_signals) - len(persisted_signals)
        ),
        new_equity_points_this_invocation=(
            len(replayed_equity) - len(persisted_equity)
        ),
        resumed=resumed,
        invalid_reason=None,
        refusal_code=None,
        final_cash_krw=state.cash_krw,
        final_position_qty=state.position_qty,
        equity_curve=tuple(replayed_equity),
        stopped_out_lockout=stopped_out_lockout,
        active_stop_price=(
            active_stop.stop_price if active_stop is not None else None
        ),
        entry_breakout_level=entry_breakout_level,
        last_processed_open_utc=last_processed_open,
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
