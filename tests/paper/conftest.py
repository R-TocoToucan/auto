"""Shared fixture builders for the paper-runner test suite.

Baseline cost model matches ``tests/backtest/test_runner.py``:

  slippage = 50 bps per side (0.50%)
  tick     = 1 KRW (test-only choice; keeps small hand-picked prices exact)
  step     = 0.001 BTC
  bid_fee  = ask_fee = 0.0025
  min_bid  = min_ask = 5000 KRW

``hysteresis_bps`` defaults to ``"75"`` — the frozen production band —
because the paper handler pins that exact value; the runner's own
``WARMUP_CANDLE_COUNT`` (1200) is independent of the strategy config's
``lookback_candles``/``warmup_candles`` fields but every fixture here
also sets those to 1200 so warmup and paper-split boundaries coincide
(matching the production baseline shape the plan requires fixtures to
mirror).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from bithumb_bot.artifact.canonical import sha256_hex
from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.bithumb_spec.snapshot import FeeRates, Minimums, SnapshotV1
from bithumb_bot.core.money import Money
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance
from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT
from bithumb_bot.strategy.config import BaselineStrategyConfig

UNIT = 240
T0 = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=UNIT)


def make_candle(idx: int, o: str, h: str, low: str, c: str) -> Candle:
    return Candle(
        market="KRW-BTC",
        unit_minutes=UNIT,
        open_time_utc=T0 + STEP * idx,
        open=o,
        high=h,
        low=low,
        close=c,
        volume="1",
        quote_volume=c,
    )


def flat(idx: int, price: str) -> Candle:
    return make_candle(idx, price, price, price, price)


def warmup_candles(price: str = "100000000") -> list[Candle]:
    """``WARMUP_CANDLE_COUNT`` flat candles — never crosses the hysteresis
    band (close == running SMA exactly), so no signal fires inside a
    plain warmup slice built this way."""
    return [flat(i, price) for i in range(WARMUP_CANDLE_COUNT)]


def make_dataset(
    candles: list[Candle], *, missing: list[str] | None = None
) -> CandleDataset:
    end = candles[-1].open_time_utc + STEP
    return CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=UNIT,
        requested_start_utc=candles[0].open_time_utc.isoformat(),
        requested_end_utc=end.isoformat(),
        fetched_at_utc="2026-01-01T00:00:00+00:00",
        candles=candles,
        missing_intervals_utc=missing or [],
        provenance=DatasetProvenance(
            source_endpoint="/v1/candles/minutes/240",
            base_url="https://api.bithumb.com",
            pages_fetched=1,
            page_cursors_kst=[],
            effective_end_utc=end.isoformat(),
        ),
    )


def make_snapshot(
    *,
    buy_status: Literal[
        "confirmed_read_only",
        "provisional_documented",
        "unresolved_until_M6B",
        "contradicted",
    ] = "confirmed_read_only",
) -> SnapshotV1:
    return SnapshotV1(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        retrieved_at_utc="2026-01-01T00:00:00Z",
        source_endpoints=["/v1/orders/chance"],
        fee_rates=FeeRates(bid="0.0025", ask="0.0025"),
        minimums=Minimums(krw_min_total_bid="5000", krw_min_total_ask="5000"),
        price_tick_rules={"default_tick": Decimal("1")},
        quantity_step_rules={"default_step": Decimal("0.001")},
        supported_order_types=["price", "market", "limit"],
        verification_status={
            "general_fee_rate": "confirmed_read_only",
            "market_buy_fee_reservation": buy_status,
            "rounding_rejection_behavior": "unresolved_until_M6B",
            "live_order_acceptance": "unresolved_until_M6B",
        },
        source_fixture_hashes=["0" * 64],
    )


def make_strategy_config(hysteresis_bps: str = "75") -> BaselineStrategyConfig:
    return BaselineStrategyConfig(
        rule_id="price_over_sma",
        ma_type="SMA",
        lookback_candles=WARMUP_CANDLE_COUNT,
        warmup_candles=WARMUP_CANDLE_COUNT,
        unit_minutes=UNIT,
        market="KRW-BTC",
        hysteresis_bps=Decimal(hysteresis_bps),
    )


def make_backtest_config(
    *,
    cash: str = "20000000",
    max_notional_krw: str = "100000000",
    allow_provisional: bool = False,
    hysteresis_bps: str = "75",
) -> BacktestConfig:
    return BacktestConfig(
        starting_cash_krw=Money(Decimal(cash)),
        target_sleeve_fraction=Decimal("1.0"),
        protective_stop_fraction=Decimal("0.10"),
        strategy=make_strategy_config(hysteresis_bps),
        execution=ExecutionConfig(
            slippage_bps_per_side=Decimal("50"),
            max_notional_krw=Money(Decimal(max_notional_krw)),
            allow_provisional_fee_model=allow_provisional,
        ),
    )


@pytest.fixture()
def paper_fixture() -> tuple[CandleDataset, SnapshotV1, BacktestConfig]:
    """``(dataset, snapshot, config)`` — 1200 flat warmup candles + a
    rising leg that triggers a LONG transition at the FIRST forward
    candle, plus one more flat forward candle so the resulting BUY
    intent has a next candle to fill against."""
    candles = warmup_candles()
    candles.append(
        make_candle(WARMUP_CANDLE_COUNT, "100000000", "200000000", "100000000", "200000000")
    )
    candles.append(flat(WARMUP_CANDLE_COUNT + 1, "200000000"))
    dataset = make_dataset(candles)
    snapshot = make_snapshot()
    config = make_backtest_config()
    return dataset, snapshot, config


@pytest.fixture()
def flat_warmup_and_forward_dataset() -> tuple[CandleDataset, SnapshotV1, BacktestConfig]:
    """``(dataset, snapshot, config)`` — pure flat throughout (warmup
    AND the single forward candle) at the same price. No signal fires
    anywhere (close == running SMA exactly at every index), so this is
    the simplest possible "no forward-triggering price move" dataset —
    used to verify the paper-start invariants (must_have truth #1)."""
    candles = warmup_candles()
    candles.append(flat(WARMUP_CANDLE_COUNT, "100000000"))
    dataset = make_dataset(candles)
    snapshot = make_snapshot()
    config = make_backtest_config()
    return dataset, snapshot, config


_STATE_DIR_ARTIFACT_NAMES = (
    "state.json",
    "state.json.sha256",
    "fills.jsonl",
    "signals.jsonl",
    "candle_fingerprints.jsonl",
)


def snapshot_state_dir_hashes(state_dir: Path) -> dict[str, str | None]:
    """Return ``{filename: sha256_hex(bytes) | None}`` for every paper
    session artifact file in ``state_dir`` (``None`` if the file is
    absent). Used by D2 adversarial tests to assert a FAILED resume
    (one that raises :class:`~bithumb_bot.errors.ProcessedPrefixMutatedError`)
    did not mutate the state-dir any further beyond whatever corruption
    the test itself injected before the resume attempt."""
    result: dict[str, str | None] = {}
    for name in _STATE_DIR_ARTIFACT_NAMES:
        path = state_dir / name
        result[name] = sha256_hex(path.read_bytes()) if path.is_file() else None
    return result
