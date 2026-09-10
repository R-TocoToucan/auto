"""`bt research backtest` handler — dataset → backtest → evaluation → report.

Offline. No credentials, no HTTP client, no broker path.

Sequence (fail-closed at every step):

1. ``validate(("research", "backtest"))`` — D-85 defense in depth.
2. Load the CandleDataset via ``load_dataset`` (sidecar verified).
3. Load the SnapshotV1 via ``load_snapshot`` (sidecar verified).
4. Load the research config via ``load_research_config`` — every
   backtest / strategy / execution field is REQUIRED; a missing key
   is a fail-closed refusal.
5. Check research-simulation readiness with the config's actual
   ``allow_provisional_fee_model`` + ``simulation_quantity_quantum``.
   Unresolved → refuse.
6. Run the existing chronological backtest.
7. Run the existing performance evaluation.
8. Serialize a canonical JSON report with a SHA-256 sidecar. Refuses
   to overwrite (D-76 via ``guard_against_overwrite``).

The report explicitly separates ``research_simulation_readiness`` from
``live_execution_readiness``. Documented buy-`price` / sell-`market`
support is never reported as live-order acceptance. All Decimal /
Money / Qty values are serialized as exact strings; timestamps as
UTC ISO-8601.
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

from bithumb_bot.config.validator import validate

log = structlog.get_logger()

_REPORT_SCHEMA_VERSION = 1


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


def _build_report(
    *,
    dataset_path: Path,
    dataset_bytes: bytes,
    snapshot_path: Path,
    snapshot_bytes: bytes,
    config_path: Path,
    config_bytes: bytes,
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
        "inputs": {
            "dataset_path": str(dataset_path),
            "dataset_sha256": sha256_hex(dataset_bytes),
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": sha256_hex(snapshot_bytes),
            "config_path": str(config_path),
            "config_sha256": sha256_hex(config_bytes),
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
            "trade_count": len(
                [e for e in backtest_result.entries if e.side == "buy"]
            ),
            "final_cash_krw": _decimal_str(
                backtest_result.final_cash_krw.value
            ),
            "final_position_qty": _decimal_str(
                backtest_result.final_position_qty.value
            ),
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
            "gross_before_fees_after_slippage_return": _serialize_value(
                performance.gross_before_fees_after_slippage_return
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
            "trade_count": performance.trade_count,
        },
    }
    return report


def handler(args: argparse.Namespace) -> int:
    """Entry point for ``bt research backtest``."""
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
        dataset_path=dataset_path,
        dataset_bytes=dataset_bytes,
        snapshot_path=snapshot_path,
        snapshot_bytes=snapshot_bytes,
        config_path=config_path,
        config_bytes=config_bytes,
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
    print(f"trade_count:                     {report['backtest']['trade_count']}")
    print(f"report_sha256[:12]:              {sha_prefix}")
    return 0


__all__ = ["handler"]
