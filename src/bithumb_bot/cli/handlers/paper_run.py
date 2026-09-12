"""`bt paper run` handler — dataset + snapshot + config -> paper session -> report.

Offline (as far as market data goes — the dataset is a pre-fetched,
public, immutable candle series). No credentials, no HTTP client, no
broker path. Mirrors ``research_backtest.py``'s structure closely
(validate-before-do, engineering-smoke pre-Gate-2 caps, canonical
report + sidecar) — see that module's docstring for the shared
rationale; this docstring only calls out what's paper-specific.

Sequence (fail-closed at every step):

1. ``validate(("paper", "run"))`` — D-85 defense in depth.
2. Refuse to overwrite ``--out`` (D-76) BEFORE any expensive work.
   ``--state-dir`` is NOT covered by this guard — it is the durable,
   append-only audit-trail root and is expected to already exist
   across invocations.
3. Load Gate 1, the ``CandleDataset``, the ``SnapshotV1``, and the
   research config — identical loaders to ``research_backtest.py``.
4. Engineering-smoke pre-Gate-2 cap enforcement — the SAME ~40-line
   block as ``research_backtest.py``, duplicated deliberately rather
   than factored into a shared helper (see the inline comment at that
   block for why).
5. Pin ``hysteresis_bps == "75"`` — the paper handler is bound to the
   frozen ``price_over_sma`` baseline's cost-derived hysteresis band;
   any other configured value refuses rather than silently running
   under a different band.
6. Check research-simulation readiness (same gate as research
   backtest).
7. Call :func:`bithumb_bot.paper.runner.run_paper_session`.
8. Build and persist a canonical JSON report + SHA-256 sidecar to
   ``--out``, distinct from the durable ``--state-dir`` audit trail.

The report carries ``run_purpose="engineering_smoke"``,
``selection_eligible=false``, ``holdout_eligible=false`` — same
engineering-smoke identity as ``bt research backtest`` — plus a
``"paper"`` block describing the warm-up/forward split and the
resume/audit-trail state.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from bithumb_bot.artifact.canonical import (
    canonical_bytes,
    guard_against_overwrite,
    sha256_hex,
    write_with_sidecar,
)
from bithumb_bot.artifact.timestamps import utc_now
from bithumb_bot.bithumb_spec.snapshot import load_snapshot
from bithumb_bot.config.gate_loader import load_gate1
from bithumb_bot.config.research_config import ResearchConfigError, load_research_config
from bithumb_bot.config.validator import REPO_ROOT_ENV, validate
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import (
    FillReplayDivergenceError,
    ForwardDatasetDivergenceError,
    PaperInputContractMismatchError,
    PaperStateDirError,
    ProcessedPrefixMutatedError,
    SidecarHashMismatchError,
)
from bithumb_bot.execution.readiness import check_execution_readiness
from bithumb_bot.market_data.dataset import load_dataset
from bithumb_bot.paper.runner import run_paper_session

_REPORT_SCHEMA_VERSION = 1
_RUN_PURPOSE_ENGINEERING_SMOKE = "engineering_smoke"
_PINNED_HYSTERESIS_BPS = Decimal("75")


def _decimal_str(d: Decimal) -> str:
    """Fixed-point exact string for a :class:`Decimal` (see research_backtest.py)."""
    return format(d, "f")


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        result: Any = value
    elif isinstance(value, Decimal):
        result = _decimal_str(value)
    elif isinstance(value, (Money, Qty)):
        result = _decimal_str(value.value)
    elif isinstance(value, datetime):
        result = value.isoformat()
    elif isinstance(value, dict):
        result = {str(k): _serialize_value(v) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        result = [_serialize_value(v) for v in value]
    else:
        result = str(value)
    return result


def _serialize_ledger_entry(entry: Any) -> dict[str, Any]:
    result: dict[str, Any] = _serialize_value(asdict(entry))
    return result


def _serialize_pending_intent(intent: Any) -> dict[str, Any] | None:
    if intent is None:
        return None
    return {
        "side": intent.side,
        "source_open_time_utc": intent.source_open_time_utc.isoformat(),
        "signal_ts_utc": intent.signal_ts_utc.isoformat(),
        "unit_minutes": intent.unit_minutes,
        "requested_notional_krw": (
            _decimal_str(intent.requested_notional_krw.value)
            if intent.requested_notional_krw is not None
            else None
        ),
        "requested_qty": (
            _decimal_str(intent.requested_qty.value)
            if intent.requested_qty is not None
            else None
        ),
    }


def _build_report(
    *,
    dataset_bytes: bytes,
    snapshot_bytes: bytes,
    config_bytes: bytes,
    gate1_source_commit: str,
    gate1_file_sha256: str,
    provisional_engineering_notional_krw: Decimal,
    dataset: Any,
    snapshot: Any,
    backtest_config: Any,
    readiness: Any,
    session: Any,
    state_json_sha256: str | None,
    generated_at_utc: str,
) -> dict[str, Any]:
    backtest_result = session.backtest_result

    report: dict[str, Any] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at_utc": generated_at_utc,
        "run_purpose": _RUN_PURPOSE_ENGINEERING_SMOKE,
        "selection_eligible": False,
        "holdout_eligible": False,
        "mode": "paper",
        "inputs": {
            "dataset_sha256": sha256_hex(dataset_bytes),
            "snapshot_sha256": sha256_hex(snapshot_bytes),
            "config_sha256": sha256_hex(config_bytes),
        },
        "provenance": {
            "gate1_source_commit": gate1_source_commit,
            "gate1_file_sha256": gate1_file_sha256,
            "provisional_engineering_notional_krw": _decimal_str(
                provisional_engineering_notional_krw
            ),
        },
        "dataset": {
            "market": dataset.market,
            "unit_minutes": dataset.unit_minutes,
            "candle_count": len(dataset.candles),
            "missing_interval_count": len(dataset.missing_intervals_utc),
            "first_open_utc": (
                dataset.candles[0].open_time_utc.isoformat()
                if dataset.candles
                else None
            ),
            "last_open_utc": (
                dataset.candles[-1].open_time_utc.isoformat()
                if dataset.candles
                else None
            ),
        },
        "snapshot": {
            "market": snapshot.market,
            "retrieved_at_utc": snapshot.retrieved_at_utc,
            "verification_status": dict(snapshot.verification_status),
        },
        "strategy": {
            "rule_id": backtest_config.strategy.rule_id,
            "ma_type": backtest_config.strategy.ma_type,
            "lookback_candles": backtest_config.strategy.lookback_candles,
            "warmup_candles": backtest_config.strategy.warmup_candles,
            "unit_minutes": backtest_config.strategy.unit_minutes,
            "market": backtest_config.strategy.market,
            "hysteresis_bps": _decimal_str(backtest_config.strategy.hysteresis_bps),
        },
        "backtest_config": {
            "starting_cash_krw": _decimal_str(backtest_config.starting_cash_krw.value),
            "target_sleeve_fraction": _decimal_str(
                backtest_config.target_sleeve_fraction
            ),
            "protective_stop_fraction": _decimal_str(
                backtest_config.protective_stop_fraction
            ),
        },
        "execution": {
            "slippage_bps_per_side": _decimal_str(
                backtest_config.execution.slippage_bps_per_side
            ),
            "max_notional_krw": _decimal_str(
                backtest_config.execution.max_notional_krw.value
            ),
            "allow_provisional_fee_model": (
                backtest_config.execution.allow_provisional_fee_model
            ),
            "simulation_quantity_quantum": (
                _decimal_str(backtest_config.execution.simulation_quantity_quantum)
                if backtest_config.execution.simulation_quantity_quantum is not None
                else None
            ),
        },
        "readiness": {
            "research_simulation_readiness": (
                readiness.research_simulation_readiness
            ),
            "live_execution_readiness": readiness.live_execution_readiness,
            "research_missing_requirements": list(
                readiness.research_missing_requirements
            ),
            "live_only_unresolved_facts": list(readiness.live_missing_requirements),
        },
        "paper": {
            "paper_start_ts_utc": (
                session.paper_start_ts_utc.isoformat()
                if session.paper_start_ts_utc is not None
                else None
            ),
            "warmup_candle_count": session.warmup_candle_count,
            "forward_candle_count": session.forward_candle_count,
            "forward_signal_count": session.forward_signal_count,
            "forward_fill_count": len(session.forward_entries),
            "new_fills_this_invocation": session.new_fills_this_invocation,
            "resumed": session.resumed,
            "state_dir_sha256_of_state_json": state_json_sha256,
        },
        "backtest": {
            "valid": session.invalid_reason is None,
            "invalid_reason": session.invalid_reason,
            "refusal_code": session.refusal_code,
            "ledger_entry_count": len(session.forward_entries),
            "final_cash_krw": (
                _decimal_str(backtest_result.final_cash_krw.value)
                if backtest_result is not None
                else None
            ),
            "final_position_qty": (
                _decimal_str(backtest_result.final_position_qty.value)
                if backtest_result is not None
                else None
            ),
            "pending_intent": (
                _serialize_pending_intent(backtest_result.pending_intent)
                if backtest_result is not None
                else None
            ),
            "stopped_out_lockout": (
                backtest_result.stopped_out_lockout
                if backtest_result is not None
                else None
            ),
            "processed_first_open_utc": (
                backtest_result.processed_first_open_utc.isoformat()
                if backtest_result is not None and backtest_result.processed_first_open_utc
                else None
            ),
            "processed_last_open_utc": (
                backtest_result.processed_last_open_utc.isoformat()
                if backtest_result is not None and backtest_result.processed_last_open_utc
                else None
            ),
        },
        "ledger": [
            _serialize_ledger_entry(entry) for entry in session.forward_entries
        ],
    }
    return report


def handler(args: Any) -> int:
    """Entry point for ``bt paper run``."""
    _result = validate(("paper", "run"))
    if not _result.ok:
        print(
            f"bt paper run: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    try:
        dataset_path = Path(args.dataset)
        snapshot_path = Path(args.snapshot)
        config_path = Path(args.config)
        state_dir = Path(args.state_dir)
        out_path = Path(args.out)
    except AttributeError as exc:
        print(f"bt paper run: missing argument ({exc})", file=sys.stderr)
        return 1

    # Refuse to overwrite `--out` BEFORE doing any expensive work.
    # `--state-dir` is deliberately NOT guarded the same way — it is the
    # durable, append-only audit-trail root the runner itself manages.
    try:
        guard_against_overwrite(
            out_path, out_path.with_name(out_path.name + ".sha256")
        )
    except Exception as exc:
        print(
            f"bt paper run: refuse to overwrite ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    repo_root = Path(os.environ.get(REPO_ROOT_ENV) or Path.cwd())
    gate1_path = repo_root / "config" / "decisions" / "gate1.toml"
    try:
        gate1, gate1_sha256 = load_gate1(gate1_path)
    except Exception as exc:
        print(
            f"bt paper run: gate1 refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        dataset = load_dataset(dataset_path)
    except Exception as exc:
        print(
            f"bt paper run: dataset refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    dataset_bytes = dataset_path.read_bytes()

    try:
        snapshot = load_snapshot(snapshot_path)
    except Exception as exc:
        print(
            f"bt paper run: snapshot refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    snapshot_bytes = snapshot_path.read_bytes()

    try:
        backtest_config = load_research_config(config_path)
    except ResearchConfigError as exc:
        print(
            f"bt paper run: config refused (ResearchConfigError): {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            f"bt paper run: config refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    config_bytes = config_path.read_bytes()

    # -------------------------------------------------------------------
    # Engineering-smoke cap enforcement (pre-Gate-2) — deliberately
    # duplicated from `research_backtest.py` rather than factored into a
    # shared helper (ponytail mode: divergent safety-critical code paths
    # sharing a helper risk a single subtle bug affecting both handlers
    # identically and silently; duplication here keeps each auditable on
    # its own).
    # -------------------------------------------------------------------
    provisional_cap = gate1.provisional_engineering_notional_krw
    if gate1.max_validated_notional_krw is not None:
        print(
            "bt paper run: refusal: Gate 2 has frozen "
            "max_validated_notional_krw; strategy evaluation requires a "
            "distinct selection handler with calibration provenance "
            "(missing: gate2_selection_handler).",
            file=sys.stderr,
        )
        return 1
    configured_cap = backtest_config.execution.max_notional_krw.value
    if configured_cap > provisional_cap:
        print(
            f"bt paper run: refusal: engineering-smoke "
            f"max_notional_krw={_decimal_str(configured_cap)} exceeds "
            f"Gate 1 provisional_engineering_notional_krw="
            f"{_decimal_str(provisional_cap)}. Pre-Gate-2 runs cannot "
            "raise the cap by TOML (missing: gate2_validated_cap).",
            file=sys.stderr,
        )
        return 1
    fee_bid = snapshot.fee_rates.bid
    target_debit = (
        backtest_config.starting_cash_krw.value
        * backtest_config.target_sleeve_fraction
    )
    intended_pre_fee = target_debit / (Decimal("1") + fee_bid)
    if intended_pre_fee > provisional_cap:
        print(
            f"bt paper run: refusal: intended pre-fee order "
            f"{_decimal_str(intended_pre_fee)} exceeds Gate 1 "
            f"provisional_engineering_notional_krw="
            f"{_decimal_str(provisional_cap)}. Reduce starting_cash_krw or "
            "target_sleeve_fraction (missing: gate2_validated_cap).",
            file=sys.stderr,
        )
        return 1

    # Paper handler is bound to the frozen `price_over_sma` baseline's
    # cost-derived 75-bp hysteresis band — any other configured value
    # refuses rather than silently running under a different band.
    if backtest_config.strategy.hysteresis_bps != _PINNED_HYSTERESIS_BPS:
        print(
            f"bt paper run: refusal: hysteresis_bps="
            f"{_decimal_str(backtest_config.strategy.hysteresis_bps)} != "
            f"{_decimal_str(_PINNED_HYSTERESIS_BPS)} — the paper handler is "
            "bound to the frozen price_over_sma baseline's cost-derived "
            "hysteresis band (missing: pinned_hysteresis_bps).",
            file=sys.stderr,
        )
        return 1

    readiness = check_execution_readiness(
        snapshot,
        allow_provisional_fee_model=(
            backtest_config.execution.allow_provisional_fee_model
        ),
        simulation_quantity_quantum=(
            backtest_config.execution.simulation_quantity_quantum
        ),
    )
    if readiness.research_simulation_readiness != "ready":
        print(
            f"bt paper run: research_simulation_readiness=unresolved; "
            f"missing={list(readiness.research_missing_requirements)!r}",
            file=sys.stderr,
        )
        return 1

    try:
        session = run_paper_session(
            dataset,
            snapshot,
            backtest_config,
            state_dir,
            now_utc=utc_now(),
        )
    except (
        PaperStateDirError,
        PaperInputContractMismatchError,
        ForwardDatasetDivergenceError,
        FillReplayDivergenceError,
        ProcessedPrefixMutatedError,
        SidecarHashMismatchError,
    ) as exc:
        print(
            f"bt paper run: refusal ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    if session.invalid_reason is not None:
        print(
            f"bt paper run: refusal ({session.refusal_code}): "
            f"{session.invalid_reason}",
            file=sys.stderr,
        )
        return 1

    state_json_path = state_dir / "state.json"
    state_json_sha256 = (
        sha256_hex(state_json_path.read_bytes())
        if state_json_path.is_file()
        else None
    )

    generated_at = utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")
    report = _build_report(
        dataset_bytes=dataset_bytes,
        snapshot_bytes=snapshot_bytes,
        config_bytes=config_bytes,
        gate1_source_commit=gate1.source_commit,
        gate1_file_sha256=gate1_sha256,
        provisional_engineering_notional_krw=provisional_cap,
        dataset=dataset,
        snapshot=snapshot,
        backtest_config=backtest_config,
        readiness=readiness,
        session=session,
        state_json_sha256=state_json_sha256,
        generated_at_utc=generated_at,
    )

    try:
        write_with_sidecar(out_path, canonical_bytes(report))
    except Exception as exc:
        print(
            f"bt paper run: report write failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    sha_prefix = sha256_hex(out_path.read_bytes())[:12]
    print(f"report:                          {out_path}")
    print(f"run_purpose:                     {_RUN_PURPOSE_ENGINEERING_SMOKE}")
    print("selection_eligible:              False")
    print("holdout_eligible:                 False")
    print(f"resumed:                         {session.resumed}")
    print(f"forward_candle_count:            {session.forward_candle_count}")
    print(f"forward_fill_count:              {len(session.forward_entries)}")
    print(f"new_fills_this_invocation:       {session.new_fills_this_invocation}")
    print(f"report_sha256[:12]:              {sha_prefix}")
    return 0


__all__ = ["handler"]
