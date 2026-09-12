"""`bt research backtest` handler — dataset → backtest → evaluation → report.

Offline. No credentials, no HTTP client, no broker path.

Sequence (fail-closed at every step):

1. ``validate(("research", "backtest"))`` — D-85 defense in depth.
2. Load the CandleDataset via ``load_dataset`` (sidecar verified).
3. Load the SnapshotV1 via ``load_snapshot`` (sidecar verified).
4. Load the research config via ``load_research_config`` — every
   backtest / strategy / execution field is REQUIRED; a missing key
   is a fail-closed refusal.
5. **Engineering-smoke pre-Gate-2 caps** — the run is treated only as
   an engineering smoke test until Gate 2 freezes
   ``max_validated_notional_krw``. Refuse if:

     * ``config.execution.max_notional_krw`` exceeds Gate 1's
       ``provisional_engineering_notional_krw`` (an arbitrary TOML
       cap cannot bypass the Gate 1 limit); OR
     * the intended pre-fee order (``starting_cash *
       target_sleeve / (1 + fee_bid)``) exceeds the provisional
       engineering notional.

6. Check research-simulation readiness with the config's actual
   ``allow_provisional_fee_model`` + ``simulation_quantity_quantum``.
   Unresolved → refuse.
7. Run the existing chronological backtest.
8. Run the existing performance evaluation.
9. Serialize a canonical JSON report with a SHA-256 sidecar. Refuses
   to overwrite (D-76 via ``guard_against_overwrite``).

The report explicitly separates ``research_simulation_readiness`` from
``live_execution_readiness``, and marks the run as
``run_purpose="engineering_smoke"`` with ``selection_eligible=false``
and ``holdout_eligible=false`` — a green run of this handler is NEVER
a selection-ready result. Machine-readable outputs persist the full
trade ledger, the equity curve, any rejected/unvalidated intents,
and audit counts (``ledger_entry_count``, ``position_entry_count``,
``closed_trade_count``). All Decimal / Money / Qty values are
serialized as exact strings; timestamps as UTC ISO-8601. No absolute
filesystem paths land in the report (no Windows username, no local
paths — inputs are identified by SHA-256 only).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from bithumb_bot.config.validator import REPO_ROOT_ENV, validate

log = structlog.get_logger()

_REPORT_SCHEMA_VERSION = 2

# Pre-Gate-2 engineering-smoke identity carried on every report this
# handler emits. Selection- / holdout-eligible reports must be
# produced by their own future handlers, backed by frozen Gate-2
# `max_validated_notional_krw`, calibration provenance, and the rest
# of the Gate-2 preconditions.
_RUN_PURPOSE_ENGINEERING_SMOKE = "engineering_smoke"


def _decimal_str(d: Decimal) -> str:
    """Fixed-point exact string for a :class:`Decimal`.

    ``str(Decimal("0.00000001"))`` yields scientific ``"1E-8"``; we
    canonicalize to ``"0.00000001"`` so operators can grep the report
    without a mental parser. ``format(d, 'f')`` is the exact
    fixed-point representation.
    """
    return format(d, "f")


def _serialize_value(value: Any) -> Any:
    """Recursively convert a value into JSON-serializable canonical form.

    * :class:`Decimal` and Money/Qty wrappers → their exact string
      (fixed-point, no scientific notation).
    * :class:`datetime` → ISO-8601 UTC.
    * dict / list / tuple → recursive.
    * everything else passes through unchanged (so bool/int/str/None
      land as themselves).
    """
    from bithumb_bot.core.money import Money, Qty

    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, Decimal):
        return _decimal_str(value)
    if isinstance(value, (Money, Qty)):
        return _decimal_str(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_value(v) for v in value]
    return str(value)


def _serialize_ledger_entry(entry: Any) -> dict[str, Any]:
    result: dict[str, Any] = _serialize_value(asdict(entry))
    return result


def _serialize_equity_point(point: Any) -> dict[str, Any]:
    result: dict[str, Any] = _serialize_value(asdict(point))
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
    backtest_result: Any,
    performance: Any,
    generated_at_utc: str,
) -> dict[str, Any]:
    from bithumb_bot.artifact.canonical import sha256_hex

    report: dict[str, Any] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at_utc": generated_at_utc,
        # Engineering-smoke identity (Section 1 of the patch): pre-Gate-2,
        # every report from this handler is smoke-only, never selection-
        # eligible, never holdout-eligible. A future selection/holdout
        # handler will emit distinct values — validators can pin these.
        "run_purpose": _RUN_PURPOSE_ENGINEERING_SMOKE,
        "selection_eligible": False,
        "holdout_eligible": False,
        "inputs": {
            "dataset_sha256": sha256_hex(dataset_bytes),
            "snapshot_sha256": sha256_hex(snapshot_bytes),
            "config_sha256": sha256_hex(config_bytes),
        },
        "provenance": {
            # Reproducible code-version identifier — Gate 1's
            # `source_commit` names the frozen decision register
            # commit; combined with the config/snapshot/dataset SHA-256
            # attestations above, this is enough to rebuild the run.
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
            "price_tick_schedule_provenance": (
                snapshot.price_tick_schedule_provenance
                if snapshot.price_tick_schedule_provenance is not None
                else None
            ),
        },
        "strategy": {
            "rule_id": backtest_config.strategy.rule_id,
            "ma_type": backtest_config.strategy.ma_type,
            "lookback_candles": backtest_config.strategy.lookback_candles,
            "warmup_candles": backtest_config.strategy.warmup_candles,
            "unit_minutes": backtest_config.strategy.unit_minutes,
            "market": backtest_config.strategy.market,
            "hysteresis_bps": _decimal_str(
                backtest_config.strategy.hysteresis_bps
            ),
            "hysteresis_application": "entry_only",
        },
        "backtest_config": {
            "starting_cash_krw": _decimal_str(
                backtest_config.starting_cash_krw.value
            ),
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
                _decimal_str(
                    backtest_config.execution.simulation_quantity_quantum
                )
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
            "live_only_unresolved_facts": list(
                readiness.live_missing_requirements
            ),
        },
        "backtest": {
            "valid": backtest_result.invalid_reason is None,
            "invalid_reason": backtest_result.invalid_reason,
            "refusal_code": backtest_result.refusal_code,
            "used_provisional_fee_model": (
                backtest_result.used_provisional_fee_model
            ),
            "ledger_entry_count": len(backtest_result.entries),
            "position_entry_count": sum(
                1 for e in backtest_result.entries if e.side == "buy"
            ),
            "closed_trade_count": performance.closed_trade_count,
            "signals_generated": backtest_result.signals_generated,
            "final_cash_krw": _decimal_str(
                backtest_result.final_cash_krw.value
            ),
            "final_position_qty": _decimal_str(
                backtest_result.final_position_qty.value
            ),
            "pending_intent": _serialize_pending_intent(
                backtest_result.pending_intent
            ),
            "pending_intent_count": (
                1 if backtest_result.pending_intent is not None else 0
            ),
            "stopped_out_lockout": backtest_result.stopped_out_lockout,
            "processed_first_open_utc": (
                backtest_result.processed_first_open_utc.isoformat()
                if backtest_result.processed_first_open_utc
                else None
            ),
            "processed_last_open_utc": (
                backtest_result.processed_last_open_utc.isoformat()
                if backtest_result.processed_last_open_utc
                else None
            ),
        },
        "performance": {
            "source_run_refused": performance.source_run_refused,
            "evaluation_invalid_reason": performance.evaluation_invalid_reason,
            "evaluation_refusal_code": performance.evaluation_refusal_code,
            "benchmark_invalid_reason": performance.benchmark_invalid_reason,
            "benchmark_refusal_code": performance.benchmark_refusal_code,
            "evaluation_first_open_utc": (
                performance.evaluation_first_open_utc.isoformat()
                if performance.evaluation_first_open_utc
                else None
            ),
            "evaluation_last_open_utc": (
                performance.evaluation_last_open_utc.isoformat()
                if performance.evaluation_last_open_utc
                else None
            ),
            "starting_equity_krw": _serialize_value(
                performance.starting_equity_krw
            ),
            "ending_mark_to_market_equity_krw": _serialize_value(
                performance.ending_mark_to_market_equity_krw
            ),
            "ending_net_liquidation_equity_krw": _serialize_value(
                performance.ending_net_liquidation_equity_krw
            ),
            "strategy_net_return": _serialize_value(
                performance.strategy_net_return
            ),
            "fee_addback_return": _serialize_value(
                performance.fee_addback_return
            ),
            "max_drawdown_fraction": _serialize_value(
                performance.max_drawdown_fraction
            ),
            "annualized_volatility": _serialize_value(
                performance.annualized_volatility
            ),
            "annualized_sharpe": _serialize_value(performance.annualized_sharpe),
            "closed_trade_win_rate": _serialize_value(
                performance.closed_trade_win_rate
            ),
            "average_holding_period_hours": _serialize_value(
                performance.average_holding_period_hours
            ),
            "turnover": _serialize_value(performance.turnover),
            "benchmark_net_return": _serialize_value(
                performance.benchmark_net_return
            ),
            "strategy_minus_benchmark_net_return": _serialize_value(
                performance.strategy_minus_benchmark_net_return
            ),
            "total_actual_fees_krw": _serialize_value(
                performance.total_actual_fees_krw
            ),
            "total_modeled_slippage_krw": _serialize_value(
                performance.total_modeled_slippage_krw
            ),
            "estimated_final_liquidation_fee_krw": _serialize_value(
                performance.estimated_final_liquidation_fee_krw
            ),
            "ledger_entry_count": performance.ledger_entry_count,
            "position_entry_count": performance.position_entry_count,
            "closed_trade_count": performance.closed_trade_count,
            "open_position_qty": _serialize_value(performance.open_position_qty),
            "pending_intent_count": performance.pending_intent_count,
            "refused_run_count": performance.refused_run_count,
            "used_provisional_fee_model": performance.used_provisional_fee_model,
            "periods_per_year": performance.periods_per_year,
            "risk_free_rate": _serialize_value(performance.risk_free_rate),
        },
        # Full trade ledger (buys + sells, in order). Serialized here
        # rather than nested under `performance` so downstream tools
        # can read it without parsing the whole report.
        "ledger": [
            _serialize_ledger_entry(entry)
            for entry in performance.entries
        ],
        # Full equity curve (post-warm-up window when valid, partial-
        # path diagnostic curve when the source refused).
        "equity_curve": [
            _serialize_equity_point(point)
            for point in performance.equity_curve
        ],
    }
    return report


def handler(args: argparse.Namespace) -> int:
    """Entry point for ``bt research backtest``."""
    import os

    _result = validate(("research", "backtest"))
    if not _result.ok:
        print(
            f"bt research backtest: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    try:
        dataset_path = Path(args.dataset)
        snapshot_path = Path(args.snapshot)
        config_path = Path(args.config)
        out_path = Path(args.out)
    except AttributeError as exc:
        print(
            f"bt research backtest: missing argument ({exc})",
            file=sys.stderr,
        )
        return 1

    # Lazy imports.
    from bithumb_bot.artifact.canonical import (
        canonical_bytes,
        guard_against_overwrite,
        write_with_sidecar,
    )
    from bithumb_bot.artifact.timestamps import utc_now
    from bithumb_bot.backtest.runner import run_backtest
    from bithumb_bot.bithumb_spec.snapshot import load_snapshot
    from bithumb_bot.config.gate_loader import load_gate1
    from bithumb_bot.config.research_config import (
        ResearchConfigError,
        load_research_config,
    )
    from bithumb_bot.evaluation.report import evaluate_backtest
    from bithumb_bot.execution.readiness import check_execution_readiness
    from bithumb_bot.market_data.dataset import load_dataset

    # Refuse to overwrite BEFORE doing any expensive work.
    try:
        guard_against_overwrite(
            out_path, out_path.with_name(out_path.name + ".sha256")
        )
    except Exception as exc:
        print(
            f"bt research backtest: refuse to overwrite ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    # Load Gate 1 — the validator has already confirmed it exists and
    # is frozen; we reload here (same `load_gate1`) to obtain the
    # frozen provisional cap and provenance stamps for the report.
    repo_root = Path(os.environ.get(REPO_ROOT_ENV) or Path.cwd())
    gate1_path = repo_root / "config" / "decisions" / "gate1.toml"
    try:
        gate1, gate1_sha256 = load_gate1(gate1_path)
    except Exception as exc:
        print(
            f"bt research backtest: gate1 refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        dataset = load_dataset(dataset_path)
    except Exception as exc:
        print(
            f"bt research backtest: dataset refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    dataset_bytes = dataset_path.read_bytes()

    try:
        snapshot = load_snapshot(snapshot_path)
    except Exception as exc:
        print(
            f"bt research backtest: snapshot refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    snapshot_bytes = snapshot_path.read_bytes()

    try:
        backtest_config = load_research_config(config_path)
    except ResearchConfigError as exc:
        print(
            f"bt research backtest: config refused (ResearchConfigError): {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            f"bt research backtest: config refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    config_bytes = config_path.read_bytes()

    # -------------------------------------------------------------------
    # Engineering-smoke cap enforcement (pre-Gate-2). The Gate-2 value
    # `max_validated_notional_krw` is deferred; until it freezes, the
    # only cap this handler recognises is the frozen Gate-1
    # `provisional_engineering_notional_krw`. An arbitrary TOML value
    # in `[execution] max_notional_krw` cannot bypass it.
    # -------------------------------------------------------------------
    provisional_cap = gate1.provisional_engineering_notional_krw
    if gate1.max_validated_notional_krw is not None:
        # If Gate 2 has actually frozen the cap, this handler is still
        # engineering-smoke-only; refuse rather than silently promoting.
        print(
            "bt research backtest: refusal: Gate 2 has frozen "
            "max_validated_notional_krw; strategy evaluation requires a "
            "distinct selection handler with calibration provenance "
            "(missing: gate2_selection_handler).",
            file=sys.stderr,
        )
        return 1
    configured_cap = backtest_config.execution.max_notional_krw.value
    if configured_cap > provisional_cap:
        print(
            f"bt research backtest: refusal: engineering-smoke "
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
            f"bt research backtest: refusal: intended pre-fee order "
            f"{_decimal_str(intended_pre_fee)} exceeds Gate 1 "
            f"provisional_engineering_notional_krw="
            f"{_decimal_str(provisional_cap)}. Reduce starting_cash_krw or "
            "target_sleeve_fraction (missing: gate2_validated_cap).",
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
            f"bt research backtest: research_simulation_readiness=unresolved; "
            f"missing={list(readiness.research_missing_requirements)!r}",
            file=sys.stderr,
        )
        return 1

    try:
        backtest_result = run_backtest(dataset, snapshot, backtest_config)
    except Exception as exc:
        print(
            f"bt research backtest: backtest crashed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    performance = evaluate_backtest(
        dataset, snapshot, backtest_config, backtest_result
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
        backtest_result=backtest_result,
        performance=performance,
        generated_at_utc=generated_at,
    )

    try:
        write_with_sidecar(out_path, canonical_bytes(report))
    except Exception as exc:
        print(
            f"bt research backtest: report write failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    from bithumb_bot.artifact.canonical import sha256_hex

    sha_prefix = sha256_hex(out_path.read_bytes())[:12]
    print(f"report:                          {out_path}")
    print(f"run_purpose:                     {_RUN_PURPOSE_ENGINEERING_SMOKE}")
    print("selection_eligible:              False")
    print("holdout_eligible:                 False")
    print(
        f"research_simulation_readiness:   "
        f"{readiness.research_simulation_readiness}"
    )
    print(
        f"live_execution_readiness:        "
        f"{readiness.live_execution_readiness}"
    )
    print(
        f"backtest_valid:                  "
        f"{'yes' if backtest_result.invalid_reason is None else 'no'}"
    )
    print(
        f"position_entry_count:            "
        f"{report['backtest']['position_entry_count']}"
    )
    print(
        f"closed_trade_count:              "
        f"{report['backtest']['closed_trade_count']}"
    )
    print(
        f"ledger_entry_count:              "
        f"{report['backtest']['ledger_entry_count']}"
    )
    print(f"report_sha256[:12]:              {sha_prefix}")
    return 0


__all__ = ["handler"]
