"""Bounded forward paper-trading runner — a thin wrapper around ``run_backtest``.

Deliberately NOT a reimplementation. Every accounting rule (fees,
slippage, tick/step rounding, min-order, notional cap, protective
stop, stopped-out lockout, readiness) already lives inside
:func:`bithumb_bot.backtest.runner.run_backtest`. This module adds
exactly three things that a pure backtest has no reason to know
about:

1. A 1,200-candle warm-up / forward split — the first
   :data:`WARMUP_CANDLE_COUNT` candles seed the SMA; only entries and
   signals whose driving candle falls at or after
   ``paper_start_ts_utc`` count as "paper" activity.
2. An incomplete-candle refusal — a candle whose close boundary has
   not yet passed relative to ``now_utc`` cannot honestly be treated
   as a completed observation, so the whole invocation refuses.
3. A restart-safe, append-only audit trail (``state.json`` +
   ``fills.jsonl`` + ``signals.jsonl``) that a second invocation over
   the same (or append-extended) dataset resumes from without
   duplicating a single line.

No-look-ahead reuse argument for the resume/replay contract
------------------------------------------------------------
``run_backtest`` is proven deterministic and prefix-stable — appending
future candles never changes a previously produced ledger entry or
signal (``tests/backtest/test_runner.py::TestNoLookAhead`` and the
analogous invariant on ``generate_signals``). This module therefore
calls ``run_backtest`` and ``generate_signals`` exactly ONCE per
invocation, over the dataset as given, and verifies that the *prefix*
of the freshly computed forward entries/signals matches the on-disk
audit trail — there is no need for a second "replay" call over a
truncated dataset to get the same guarantee.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.backtest.runner import BacktestResult, run_backtest
from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import ForwardDatasetDivergenceError, PaperStateDirError
from bithumb_bot.execution.ledger import LedgerEntry
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.paper.state import PaperState, append_jsonl, load_state, read_jsonl, save_state
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

    ``backtest_result`` is the underlying :class:`~bithumb_bot.backtest.
    runner.BacktestResult` (``None`` only when the run refused before
    reaching the engine, e.g. insufficient candles or an incomplete
    final candle) — callers needing final cash/position, pending
    intent, active stop, or lockout state read it from there rather
    than duplicating those fields here.
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
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (Money, Qty)):
        return format(value.value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_value(v) for v in value]
    return str(value)


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
       candles (refusal via the returned result, not an exception —
       mirrors :func:`~bithumb_bot.backtest.runner.run_backtest`'s own
       ``invalid_reason``/``refusal_code`` shape).
    3. Every candle's close boundary must be ``<= now_utc`` — an
       "incomplete" final candle refuses the same way.
    4. If a prior ``state.json`` exists, every resume precondition
       (warm-up hash, ``paper_start_ts_utc``, market, unit, hysteresis
       band, config/snapshot content hashes) must match exactly, else
       :class:`~bithumb_bot.errors.ForwardDatasetDivergenceError`.
    5. ``run_backtest`` runs exactly once. A domain refusal there
       propagates via the returned result — nothing is written.
    6. The previously-recorded prefix of forward entries/signals MUST
       replay byte-for-byte from the freshly computed forward
       entries/signals, else
       :class:`~bithumb_bot.errors.FillReplayDivergenceError` (raised
       from :func:`bithumb_bot.paper.state.read_jsonl`'s caller here —
       see the module's no-look-ahead reuse argument).
    7. Only the NEW entries/signals beyond that prefix are appended;
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
    for candle in dataset.candles:
        offset = candle.open_time_utc.utcoffset()
        if candle.open_time_utc.tzinfo is None or offset != timedelta(0):
            raise ValueError(
                f"candle.open_time_utc {candle.open_time_utc!r} is not "
                "tz-aware UTC (defence-in-depth; run_backtest also checks)"
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

    warmup_slice = dataset.candles[:WARMUP_CANDLE_COUNT]
    from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex

    warmup_sha256 = sha256_hex(
        canonical_bytes([c.model_dump(mode="json") for c in warmup_slice])
    )
    config_sha256 = sha256_hex(canonical_bytes(_config_fingerprint(config)))
    snapshot_sha256 = sha256_hex(canonical_bytes(snapshot.model_dump(mode="json")))
    hysteresis_str = format(config.strategy.hysteresis_bps, "f")

    prior_state = load_state(state_dir)
    resumed = prior_state is not None
    prior_fill_count = 0
    prior_signal_count = 0
    if prior_state is not None:
        mismatches = [
            name
            for name, ok in (
                ("warmup_sha256", prior_state.warmup_sha256 == warmup_sha256),
                (
                    "paper_start_ts_utc",
                    prior_state.paper_start_ts_utc == paper_start_ts_utc.isoformat(),
                ),
                ("dataset_market", prior_state.dataset_market == dataset.market),
                ("unit_minutes", prior_state.unit_minutes == dataset.unit_minutes),
                ("hysteresis_bps", prior_state.hysteresis_bps == hysteresis_str),
                ("config_sha256", prior_state.config_sha256 == config_sha256),
                ("snapshot_sha256", prior_state.snapshot_sha256 == snapshot_sha256),
            )
            if not ok
        ]
        if mismatches:
            raise ForwardDatasetDivergenceError(
                f"resumed paper session at {state_dir!s} diverges from the "
                f"current invocation on: {mismatches!r} — refusing to "
                "continue an audit trail whose inputs changed"
            )
        prior_fill_count = prior_state.forward_fill_count
        prior_signal_count = prior_state.forward_signal_count

    backtest_result = run_backtest(dataset, snapshot, config)
    if backtest_result.invalid_reason is not None:
        return PaperSessionResult(
            paper_start_ts_utc=paper_start_ts_utc,
            warmup_candle_count=WARMUP_CANDLE_COUNT,
            forward_candle_count=forward_candle_count,
            forward_entries=(),
            forward_signal_count=0,
            new_fills_this_invocation=0,
            resumed=resumed,
            invalid_reason=backtest_result.invalid_reason,
            refusal_code=backtest_result.refusal_code,
            backtest_result=backtest_result,
        )

    forward_entries = tuple(
        e for e in backtest_result.entries if e.source_open_time_utc >= paper_start_ts_utc
    )

    fills_path = state_dir / "fills.jsonl"
    on_disk_fills = read_jsonl(fills_path)
    recomputed_prior_fills = [
        _serialize_ledger_entry(e) for e in forward_entries[:prior_fill_count]
    ]
    _assert_prefix_replay(recomputed_prior_fills, on_disk_fills, fills_path)
    new_entries = forward_entries[prior_fill_count:]

    all_signals = generate_signals(dataset.candles, config.strategy)
    forward_signals = tuple(
        s for s in all_signals if s.source_open_time_utc >= paper_start_ts_utc
    )
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
        schema_version=1,
        run_purpose="engineering_smoke",
        selection_eligible=False,
        holdout_eligible=False,
        hysteresis_bps=hysteresis_str,
        paper_start_ts_utc=paper_start_ts_utc.isoformat(),
        warmup_first_open_utc=warmup_slice[0].open_time_utc.isoformat(),
        warmup_last_open_utc=warmup_slice[-1].open_time_utc.isoformat(),
        warmup_candles=WARMUP_CANDLE_COUNT,
        warmup_sha256=warmup_sha256,
        config_sha256=config_sha256,
        snapshot_sha256=snapshot_sha256,
        dataset_market=dataset.market,
        unit_minutes=dataset.unit_minutes,
        last_processed_open_utc=dataset.candles[-1].open_time_utc.isoformat(),
        forward_candle_count=forward_candle_count,
        forward_signal_count=len(forward_signals),
        forward_fill_count=len(forward_entries),
        final_cash_krw=format(backtest_result.final_cash_krw.value, "f"),
        final_position_qty=format(backtest_result.final_position_qty.value, "f"),
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
        backtest_result=backtest_result,
    )


def _assert_prefix_replay(
    recomputed: list[dict[str, Any]], on_disk: list[dict[str, Any]], path: Path
) -> None:
    from bithumb_bot.errors import FillReplayDivergenceError

    if recomputed != on_disk:
        raise FillReplayDivergenceError(
            f"replaying the engine/strategy over the recorded forward "
            f"window did NOT reproduce {path!s} byte-for-byte — refusing "
            "to extend a possibly-corrupted audit trail"
        )


__all__ = ["WARMUP_CANDLE_COUNT", "PaperSessionResult", "run_paper_session"]
