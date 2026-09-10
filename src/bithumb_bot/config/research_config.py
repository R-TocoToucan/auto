"""Strict TOML loader for `bt research backtest` config files.

Every field is REQUIRED. No CLI defaults, no silent fallbacks. A
missing key is a fail-closed refusal so the operator sees exactly
which value they forgot to supply.

TOML shape:

    [backtest]
    starting_cash_krw = "10000000"
    target_sleeve_fraction = "1.0"
    protective_stop_fraction = "0.10"

    [strategy]
    rule_id = "price_over_sma"
    ma_type = "SMA"
    lookback_candles = 1200
    warmup_candles = 1200
    unit_minutes = 240
    market = "KRW-BTC"

    [execution]
    slippage_bps_per_side = "50"
    max_notional_krw = "100000000"
    allow_provisional_fee_model = true
    simulation_quantity_quantum = "0.00000001"

All Decimal-valued fields MUST be TOML strings so they land as
:class:`~decimal.Decimal` without a float in between (D-49).
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.core.money import Money
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.strategy.config import BaselineStrategyConfig


class ResearchConfigError(ValueError):
    """Raised when a research backtest config is missing or malformed."""


_REQUIRED_SECTIONS = ("backtest", "strategy", "execution")

_REQUIRED_BACKTEST_KEYS = (
    "starting_cash_krw",
    "target_sleeve_fraction",
    "protective_stop_fraction",
)
_REQUIRED_STRATEGY_KEYS = (
    "rule_id",
    "ma_type",
    "lookback_candles",
    "warmup_candles",
    "unit_minutes",
    "market",
)
_REQUIRED_EXECUTION_KEYS = (
    "slippage_bps_per_side",
    "max_notional_krw",
    "allow_provisional_fee_model",
    "simulation_quantity_quantum",
)


def _require_str_decimal(section: str, key: str, raw: Any) -> Decimal:
    if not isinstance(raw, str):
        raise ResearchConfigError(
            f"[{section}].{key} must be a TOML string (Decimal-safe), "
            f"got {type(raw).__name__!r}"
        )
    try:
        return Decimal(raw)
    except Exception as exc:
        raise ResearchConfigError(
            f"[{section}].{key}={raw!r} does not parse as a Decimal: {exc}"
        ) from exc


def _require_int(section: str, key: str, raw: Any) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ResearchConfigError(
            f"[{section}].{key} must be an integer, got {type(raw).__name__!r}"
        )
    return raw


def _require_bool(section: str, key: str, raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise ResearchConfigError(
            f"[{section}].{key} must be a bool, got {type(raw).__name__!r}"
        )
    return raw


def _require_str(section: str, key: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        raise ResearchConfigError(
            f"[{section}].{key} must be a non-empty string, got {raw!r}"
        )
    return raw


def _check_section(name: str, section: dict[str, Any], required: tuple[str, ...]) -> None:
    if not isinstance(section, dict):
        raise ResearchConfigError(
            f"[{name}] must be a TOML table, got {type(section).__name__!r}"
        )
    missing = tuple(k for k in required if k not in section)
    if missing:
        raise ResearchConfigError(
            f"[{name}] missing required keys: {list(missing)!r}"
        )
    extra = tuple(k for k in section.keys() if k not in required)
    if extra:
        raise ResearchConfigError(
            f"[{name}] contains unrecognized keys: {sorted(extra)!r}"
        )


def load_research_config(path: Path) -> BacktestConfig:
    """Load and validate a research backtest config from ``path``.

    Every required field is checked. Returns a fully-frozen
    :class:`BacktestConfig`. Any missing key or bad type raises
    :class:`ResearchConfigError` naming the offending section/key —
    the CLI translates this into a clean stderr refusal.
    """
    if not path.is_file():
        raise ResearchConfigError(f"research config file not found: {path!r}")
    raw_bytes = path.read_bytes()
    try:
        parsed = tomllib.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ResearchConfigError(
            f"research config {path!r} is not valid TOML: {exc}"
        ) from exc

    for section in _REQUIRED_SECTIONS:
        if section not in parsed:
            raise ResearchConfigError(
                f"research config missing required section [{section}]"
            )
    _check_section("backtest", parsed["backtest"], _REQUIRED_BACKTEST_KEYS)
    _check_section("strategy", parsed["strategy"], _REQUIRED_STRATEGY_KEYS)
    _check_section("execution", parsed["execution"], _REQUIRED_EXECUTION_KEYS)

    bt = parsed["backtest"]
    strat = parsed["strategy"]
    exe = parsed["execution"]

    starting_cash = _require_str_decimal(
        "backtest", "starting_cash_krw", bt["starting_cash_krw"]
    )
    sleeve = _require_str_decimal(
        "backtest", "target_sleeve_fraction", bt["target_sleeve_fraction"]
    )
    stop_frac = _require_str_decimal(
        "backtest", "protective_stop_fraction", bt["protective_stop_fraction"]
    )

    slippage = _require_str_decimal(
        "execution", "slippage_bps_per_side", exe["slippage_bps_per_side"]
    )
    max_notional = _require_str_decimal(
        "execution", "max_notional_krw", exe["max_notional_krw"]
    )
    allow_provisional = _require_bool(
        "execution", "allow_provisional_fee_model", exe["allow_provisional_fee_model"]
    )
    quantum = _require_str_decimal(
        "execution", "simulation_quantity_quantum", exe["simulation_quantity_quantum"]
    )

    strategy = BaselineStrategyConfig(
        rule_id=_require_str("strategy", "rule_id", strat["rule_id"]),  # type: ignore[arg-type]
        ma_type=_require_str("strategy", "ma_type", strat["ma_type"]),  # type: ignore[arg-type]
        lookback_candles=_require_int(
            "strategy", "lookback_candles", strat["lookback_candles"]
        ),
        warmup_candles=_require_int(
            "strategy", "warmup_candles", strat["warmup_candles"]
        ),
        unit_minutes=_require_int(
            "strategy", "unit_minutes", strat["unit_minutes"]
        ),
        market=_require_str("strategy", "market", strat["market"]),
    )

    execution = ExecutionConfig(
        slippage_bps_per_side=slippage,
        max_notional_krw=Money(max_notional),
        allow_provisional_fee_model=allow_provisional,
        simulation_quantity_quantum=quantum,
    )

    return BacktestConfig(
        starting_cash_krw=Money(starting_cash),
        target_sleeve_fraction=sleeve,
        protective_stop_fraction=stop_frac,
        strategy=strategy,
        execution=execution,
    )


__all__ = ["ResearchConfigError", "load_research_config"]
