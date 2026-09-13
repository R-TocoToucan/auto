"""`bt paper breakout-run` handler — shadow BTC breakout candidate.

Offline research-only runner for the entry-only, exit-at-max(entry,60-low)
breakout candidate. Uses a fully separate state directory, ledger,
fills log, signals log, and equity trace from ``bt paper run``, so
both candidates can be operated in parallel without touching each
other's audit trail.

Never loads trade credentials, never imports :mod:`bithumb_bot.broker`,
never calls a live-order endpoint. Every report is stamped:

    run_purpose         = "engineering_smoke"
    selection_eligible  = false
    holdout_eligible    = false
    entry_buffer_bps    = "50"
    entry_lookback_candles = 120
    exit_lookback_candles  = 60

Sequence:

1. ``validate(("paper", "breakout-run"))`` — D-85 defense in depth.
2. Refuse to overwrite ``--out`` (D-76) BEFORE any expensive work.
3. Load Gate 1 (for provenance), dataset, snapshot.
4. Build an :class:`~bithumb_bot.execution.config.ExecutionConfig` from
   ``--starting-cash-krw`` / ``--max-notional-krw`` / a fixed 50-bps
   per-side slippage.
5. Run :func:`bithumb_bot.paper.breakout_runner.run_breakout_paper_session`.
6. Write canonical JSON report + SHA-256 sidecar.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
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
from bithumb_bot.config.validator import REPO_ROOT_ENV, validate
from bithumb_bot.core.money import Money
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.execution.readiness import check_execution_readiness
from bithumb_bot.market_data.dataset import load_dataset
from bithumb_bot.paper.breakout_runner import (
    BreakoutInputContractMismatchError,
    BreakoutPaperConfig,
    BreakoutResumeDivergenceError,
    BreakoutRunnerError,
    run_breakout_paper_session,
)
from bithumb_bot.strategy.breakout import (
    ENTRY_BUFFER_BPS,
    ENTRY_LOOKBACK_CANDLES,
    EXIT_LOOKBACK_CANDLES,
)

_REPORT_SCHEMA_VERSION = 1
_RUN_PURPOSE_ENGINEERING_SMOKE = "engineering_smoke"
#: Fixed 25-bps per-side execution slippage for the shadow candidate.
#: This is independent of the strategy's ``ENTRY_BUFFER_BPS`` (50 bps)
#: — different physical concept: the 50-bps entry buffer is a strategy
#: decision rule; the 25-bps slippage is an execution cost estimate.
#: They must not be confused or held equal by accident.
_FIXED_SLIPPAGE_BPS: Decimal = Decimal("25")


def _dstr(d: Decimal) -> str:
    return format(d, "f")


def _parse_decimal_arg(name: str, raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SystemExit(
            f"bt paper breakout-run: {name}={raw!r} is not a valid Decimal ({exc})"
        )


def _build_report(
    *,
    dataset_bytes: bytes,
    snapshot_bytes: bytes,
    gate1_source_commit: str,
    gate1_file_sha256: str,
    provisional_engineering_notional_krw: Decimal,
    dataset: Any,
    snapshot: Any,
    config: BreakoutPaperConfig,
    readiness: Any,
    session: Any,
    generated_at_utc: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at_utc": generated_at_utc,
        "run_purpose": _RUN_PURPOSE_ENGINEERING_SMOKE,
        "selection_eligible": False,
        "holdout_eligible": False,
        "notional_scale_status": "unvalidated_engineering",
        "mode": "paper_breakout_shadow",
        "inputs": {
            "dataset_sha256": sha256_hex(dataset_bytes),
            "snapshot_sha256": sha256_hex(snapshot_bytes),
        },
        "provenance": {
            "gate1_source_commit": gate1_source_commit,
            "gate1_file_sha256": gate1_file_sha256,
            "provisional_engineering_notional_krw": _dstr(
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
            "rule_id": "breakout_prior_high_with_entry_buffer",
            "market": "KRW-BTC",
            "unit_minutes": 240,
            "entry_buffer_bps": _dstr(ENTRY_BUFFER_BPS),
            "entry_lookback_candles": ENTRY_LOOKBACK_CANDLES,
            "exit_lookback_candles": EXIT_LOOKBACK_CANDLES,
        },
        "backtest_config": {
            "starting_cash_krw": _dstr(config.starting_cash_krw.value),
            "target_sleeve_fraction": "1.0",
            "protective_stop_fraction": "0.10",
        },
        "execution": {
            "slippage_bps_per_side": _dstr(config.execution.slippage_bps_per_side),
            "max_notional_krw": _dstr(config.execution.max_notional_krw.value),
            "allow_provisional_fee_model": (
                config.execution.allow_provisional_fee_model
            ),
            "simulation_quantity_quantum": (
                _dstr(config.execution.simulation_quantity_quantum)
                if config.execution.simulation_quantity_quantum is not None
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
        "paper_breakout": {
            "paper_start_ts_utc": (
                session.paper_start_ts_utc.isoformat()
                if session.paper_start_ts_utc is not None
                else None
            ),
            "warmup_candle_count": session.warmup_candle_count,
            "forward_candle_count": session.forward_candle_count,
            "forward_signal_count": session.forward_signal_count,
            "new_fills_this_invocation": session.new_fills_this_invocation,
            "new_signals_this_invocation": session.new_signals_this_invocation,
            "resumed": session.resumed,
            "stopped_out_lockout": session.stopped_out_lockout,
            "active_stop_price": (
                _dstr(session.active_stop_price.value)
                if session.active_stop_price is not None
                else None
            ),
            "entry_breakout_level": (
                _dstr(session.entry_breakout_level)
                if session.entry_breakout_level is not None
                else None
            ),
        },
        "backtest": {
            "valid": session.invalid_reason is None,
            "invalid_reason": session.invalid_reason,
            "refusal_code": session.refusal_code,
            "ledger_entry_count": len(session.forward_entries),
            "final_cash_krw": _dstr(session.final_cash_krw.value),
            "final_position_qty": _dstr(session.final_position_qty.value),
        },
        "ledger": [_serialize_entry(e) for e in session.forward_entries],
        "equity_curve": [
            _serialize_equity(p) for p in session.equity_curve
        ],
    }
    return report


def _serialize_entry(entry: Any) -> dict[str, Any]:
    from dataclasses import asdict

    def _conv(value: Any) -> Any:
        if value is None or isinstance(value, (bool, str, int)):
            return value
        if isinstance(value, Decimal):
            return _dstr(value)
        if hasattr(value, "value") and isinstance(value.value, Decimal):
            return _dstr(value.value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): _conv(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_conv(v) for v in value]
        return str(value)

    return _conv(asdict(entry))  # type: ignore[no-any-return]


def _serialize_equity(point: Any) -> dict[str, Any]:
    return {
        "candle_open_time_utc": point.candle_open_time_utc.isoformat(),
        "cash_krw": _dstr(point.cash_krw.value),
        "position_qty": _dstr(point.position_qty.value),
        "close_price": _dstr(point.close_price.value),
        "mark_to_market_equity_krw": _dstr(
            point.mark_to_market_equity_krw.value
        ),
    }


def handler(args: argparse.Namespace) -> int:
    """Entry point for ``bt paper breakout-run``."""
    _result = validate(("paper", "breakout-run"))
    if not _result.ok:
        print(
            f"bt paper breakout-run: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    try:
        dataset_path = Path(args.dataset)
        snapshot_path = Path(args.snapshot)
        state_dir = Path(args.state_dir)
        out_path = Path(args.out)
        starting_cash_raw = args.starting_cash_krw
        max_notional_raw = args.max_notional_krw
    except AttributeError as exc:
        print(
            f"bt paper breakout-run: missing argument ({exc})",
            file=sys.stderr,
        )
        return 1

    try:
        guard_against_overwrite(
            out_path, out_path.with_name(out_path.name + ".sha256")
        )
    except Exception as exc:
        print(
            f"bt paper breakout-run: refuse to overwrite "
            f"({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    repo_root = Path(os.environ.get(REPO_ROOT_ENV) or Path.cwd())
    gate1_path = repo_root / "config" / "decisions" / "gate1.toml"
    try:
        gate1, gate1_sha256 = load_gate1(gate1_path)
    except Exception as exc:
        print(
            f"bt paper breakout-run: gate1 refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        dataset = load_dataset(dataset_path)
    except Exception as exc:
        print(
            f"bt paper breakout-run: dataset refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    dataset_bytes = dataset_path.read_bytes()

    try:
        snapshot = load_snapshot(snapshot_path)
    except Exception as exc:
        print(
            f"bt paper breakout-run: snapshot refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    snapshot_bytes = snapshot_path.read_bytes()

    starting_cash = _parse_decimal_arg(
        "--starting-cash-krw", starting_cash_raw
    )
    max_notional = _parse_decimal_arg(
        "--max-notional-krw", max_notional_raw
    )
    if starting_cash <= 0:
        print(
            f"bt paper breakout-run: --starting-cash-krw must be > 0, got "
            f"{starting_cash}",
            file=sys.stderr,
        )
        return 1
    if max_notional <= 0:
        print(
            f"bt paper breakout-run: --max-notional-krw must be > 0, got "
            f"{max_notional}",
            file=sys.stderr,
        )
        return 1

    execution = ExecutionConfig(
        slippage_bps_per_side=_FIXED_SLIPPAGE_BPS,
        max_notional_krw=Money(max_notional),
        allow_provisional_fee_model=True,
        simulation_quantity_quantum=Decimal("0.00000001"),
    )
    config = BreakoutPaperConfig(
        starting_cash_krw=Money(starting_cash),
        max_notional_krw=Money(max_notional),
        execution=execution,
    )

    readiness = check_execution_readiness(
        snapshot,
        allow_provisional_fee_model=execution.allow_provisional_fee_model,
        simulation_quantity_quantum=execution.simulation_quantity_quantum,
    )
    if readiness.research_simulation_readiness != "ready":
        print(
            f"bt paper breakout-run: research_simulation_readiness=unresolved; "
            f"missing={list(readiness.research_missing_requirements)!r}",
            file=sys.stderr,
        )
        return 1

    try:
        session = run_breakout_paper_session(
            dataset, snapshot, config, state_dir, now_utc=utc_now()
        )
    except (
        BreakoutInputContractMismatchError,
        BreakoutResumeDivergenceError,
        BreakoutRunnerError,
    ) as exc:
        print(
            f"bt paper breakout-run: refusal ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    if session.invalid_reason is not None:
        print(
            f"bt paper breakout-run: refusal ({session.refusal_code}): "
            f"{session.invalid_reason}",
            file=sys.stderr,
        )
        return 1

    generated_at = utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")
    report = _build_report(
        dataset_bytes=dataset_bytes,
        snapshot_bytes=snapshot_bytes,
        gate1_source_commit=gate1.source_commit,
        gate1_file_sha256=gate1_sha256,
        provisional_engineering_notional_krw=(
            gate1.provisional_engineering_notional_krw
        ),
        dataset=dataset,
        snapshot=snapshot,
        config=config,
        readiness=readiness,
        session=session,
        generated_at_utc=generated_at,
    )

    try:
        write_with_sidecar(out_path, canonical_bytes(report))
    except Exception as exc:
        print(
            f"bt paper breakout-run: report write failed "
            f"({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    sha_prefix = sha256_hex(out_path.read_bytes())[:12]
    print(f"report:                          {out_path}")
    print(f"run_purpose:                     {_RUN_PURPOSE_ENGINEERING_SMOKE}")
    print("selection_eligible:              False")
    print("holdout_eligible:                 False")
    print(f"entry_buffer_bps:                {_dstr(ENTRY_BUFFER_BPS)}")
    print(f"entry_lookback_candles:          {ENTRY_LOOKBACK_CANDLES}")
    print(f"exit_lookback_candles:           {EXIT_LOOKBACK_CANDLES}")
    print(f"resumed:                         {session.resumed}")
    print(f"forward_candle_count:            {session.forward_candle_count}")
    print(f"forward_fill_count:              {len(session.forward_entries)}")
    print(f"new_fills_this_invocation:       {session.new_fills_this_invocation}")
    print(f"report_sha256[:12]:              {sha_prefix}")
    return 0


__all__ = ["handler"]
