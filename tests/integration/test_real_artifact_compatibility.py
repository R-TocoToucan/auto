"""End-to-end: does the real-shape sanitized snapshot drive the simulator?

Uses ONLY the committed CI-safe sanitized fixture at
``tests/fixtures/bithumb/sanitized/orders_chance/observed_krw_btc.json``
— NO dependency on any operator-local ``artifacts/spec_snapshots/**``
path.

Batch 1B activates the end-to-end body:

* :class:`TestReadinessDiagnostic` — always runs. After Batch 1B the
  real-shape snapshot's ``research_simulation_readiness`` is
  ``ready`` under an explicit :class:`ExecutionConfig` (provisional-
  fee opt-in + research quantum), while ``live_execution_readiness``
  remains ``unresolved`` (M6B facts still open).
* :class:`TestExecutableCompatibility` — runs buy → sell →
  chronological backtest → performance evaluation on integer-JSON-
  shaped candles WITHOUT monkey-patching tick / fee / minimum /
  order-type support / quantity values. Research configuration
  supplies the quantum and provisional-fee opt-in explicitly.

The test's live-only refusal is asserted separately, so an accidental
promotion of ``live_execution_readiness`` (e.g. someone marks
``live_order_acceptance`` confirmed) fails the test loudly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import sha256_hex
from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.backtest.runner import run_backtest
from bithumb_bot.bithumb_spec.sanitize import sanitize_orders_chance
from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.snapshot import (
    build_snapshot,
    load_snapshot,
    write_snapshot_with_sidecar,
)
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.core.money import Money
from bithumb_bot.evaluation.report import evaluate_backtest
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.execution.readiness import check_execution_readiness
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance
from bithumb_bot.strategy.config import BaselineStrategyConfig


FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "fixtures"
    / "bithumb"
    / "sanitized"
    / "orders_chance"
)
OBSERVED_JSON = FIXTURE_DIR / "observed_krw_btc.json"
OBSERVED_SIDECAR = FIXTURE_DIR / "observed_krw_btc.json.sha256"


# Bitcoin base accounting unit — research assumption, NOT a claim about
# Bithumb's accepted live order-volume step (still unresolved).
_RESEARCH_BTC_QUANTUM = Decimal("0.00000001")


def _research_execution_config() -> ExecutionConfig:
    """Explicit research configuration.

    * ``allow_provisional_fee_model=True`` — required to run under the
      snapshot's ``provisional_documented`` market-buy-fee-reservation
      status (still unresolved for live).
    * ``simulation_quantity_quantum=1 satoshi`` — Bitcoin base unit
      floor; the simulator retains any residual as dust.
    """
    return ExecutionConfig(
        slippage_bps_per_side=Decimal("50"),
        max_notional_krw=Money(Decimal("100_000_000")),
        allow_provisional_fee_model=True,
        simulation_quantity_quantum=_RESEARCH_BTC_QUANTUM,
    )


@pytest.fixture()
def _validate_ok(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))


def _assert_no_forbidden_fields(sanitized: dict[str, object]) -> None:
    forbidden = {
        "access_key",
        "secret_key",
        "Authorization",
        "Set-Cookie",
        "nonce",
        "signature",
        "account_id",
        "balance",
        "avg_buy_price",
        "locked_balance",
    }
    leaked = forbidden.intersection(sanitized.keys())
    assert not leaked, f"forbidden fields leaked into fixture: {sorted(leaked)!r}"


def _load_real_shape_snapshot(tmp_path: Path):
    raw = json.loads(OBSERVED_JSON.read_text(encoding="utf-8"))
    sanitized = sanitize_orders_chance(raw)
    parsed = OrdersChanceResponse.model_validate(sanitized)
    snapshot = build_snapshot(
        parsed, market="KRW-BTC", fixture_paths=[OBSERVED_JSON]
    )
    target = tmp_path / "snap.json"
    write_snapshot_with_sidecar(snapshot, target)
    return load_snapshot(target)


class TestReadinessDiagnostic:
    """Batch 1B research surface passes; live surface stays unresolved."""

    def test_committed_fixture_and_sidecar_are_tracked(self) -> None:
        assert OBSERVED_JSON.is_file()
        assert OBSERVED_SIDECAR.is_file()

    def test_fixture_sidecar_matches_bytes(self) -> None:
        recorded = OBSERVED_SIDECAR.read_text(encoding="utf-8").strip().split()
        assert len(recorded) >= 1 and len(recorded[0]) == 64
        assert recorded[0] == sha256_hex(OBSERVED_JSON.read_bytes())

    def test_fixture_contains_no_forbidden_fields(self) -> None:
        raw = json.loads(OBSERVED_JSON.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        _assert_no_forbidden_fields(raw)

    def test_research_surface_ready_under_explicit_config(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        loaded = _load_real_shape_snapshot(tmp_path)
        assert loaded.market == "KRW-BTC"
        assert loaded.fee_rates.bid == Decimal("0.0025")
        assert loaded.fee_rates.ask == Decimal("0.0025")
        assert loaded.minimums.krw_min_total_bid == Decimal("5000")
        assert loaded.minimums.krw_min_total_ask == Decimal("5000")
        assert loaded.price_tick_schedule_provenance is not None

        cfg = _research_execution_config()
        report = check_execution_readiness(
            loaded,
            allow_provisional_fee_model=cfg.allow_provisional_fee_model,
            simulation_quantity_quantum=cfg.simulation_quantity_quantum,
        )
        assert report.research_simulation_readiness == "ready"
        assert report.research_missing_requirements == ()

    def test_live_surface_remains_unresolved(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        loaded = _load_real_shape_snapshot(tmp_path)
        report = check_execution_readiness(
            loaded,
            allow_provisional_fee_model=False,
            simulation_quantity_quantum=None,
        )
        assert report.live_execution_readiness == "unresolved"
        # M6B blockers must remain surfaced.
        for expected in (
            "market_buy_fee_reservation",
            "default_step",
            "market_buy_price_support",
            "market_sell_market_support",
            "live_order_acceptance",
        ):
            assert expected in report.live_missing_requirements


# ---------------------------------------------------------------------------
# End-to-end: real-shape snapshot drives the simulator (Batch 1B)
# ---------------------------------------------------------------------------


def _integer_ohlc_candle(
    open_time: datetime,
    *,
    market: str = "KRW-BTC",
    unit_minutes: int = 240,
    open_: int,
    high: int,
    low: int,
    close: int,
    volume: str,
    quote_volume: str,
) -> Candle:
    """Build a Candle whose OHLC fields land as JSON integers.

    ``open_/high/low/close`` are int inputs — the FIX-01 code path in
    ``market_data.candles`` accepts these and wraps them as Decimal.
    """
    return Candle(
        market=market,
        unit_minutes=unit_minutes,
        open_time_utc=open_time,
        open=open_,  # int accepted, wrapped as Decimal
        high=high,
        low=low,
        close=close,
        volume=volume,
        quote_volume=quote_volume,
    )


def _build_integer_dataset() -> CandleDataset:
    """Build a small deterministic 240-minute dataset of integer OHLC
    candles that produces at least one LONG then CASH transition under
    a 3-candle SMA baseline.
    """
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    step = timedelta(minutes=240)
    # 3-candle SMA warm-up, then LONG cross, then CASH cross. Prices
    # chosen so `close_t * 3 > sum` on candle index 2 (LONG cross) and
    # then `close_t * 3 < sum` a few candles later (CASH cross).
    # Every OHLC value is a JSON integer to exercise FIX-01.
    price_series: list[tuple[int, int, int, int]] = [
        # (open, high, low, close)
        (100_000_000, 100_000_000, 100_000_000, 100_000_000),
        (100_000_000, 100_000_000, 100_000_000, 100_000_000),
        (100_000_000, 105_000_000, 99_000_000, 104_000_000),  # LONG cross
        (104_000_000, 106_000_000, 103_000_000, 105_000_000),
        (105_000_000, 105_000_000, 100_000_000, 100_000_000),  # CASH cross
        (100_000_000, 101_000_000, 99_000_000, 100_000_000),
    ]
    candles: list[Candle] = []
    for i, (o, h, low, c) in enumerate(price_series):
        candles.append(
            _integer_ohlc_candle(
                open_time=start + i * step,
                open_=o,
                high=h,
                low=low,
                close=c,
                volume="1.5",
                quote_volume="150000000",
            )
        )

    end = candles[-1].open_time_utc + step
    return CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=240,
        requested_start_utc=candles[0].open_time_utc.isoformat(),
        requested_end_utc=end.isoformat(),
        fetched_at_utc="2026-09-10T00:00:00+00:00",
        candles=candles,
        missing_intervals_utc=[],
        provenance=DatasetProvenance(
            source_endpoint="/v1/candles/minutes/240",
            base_url="https://api.bithumb.com",
            pages_fetched=1,
            page_cursors_kst=["2026-01-02T09:00:00+09:00"],
            effective_end_utc=end.isoformat(),
        ),
    )


class TestExecutableCompatibility:
    """End-to-end: real-shape snapshot + integer JSON candles drive the
    simulator through buy → sell → backtest → evaluation.

    No monkey-patching of tick / step / fees / min / order support.
    Every research assumption is supplied via configuration.
    """

    def test_end_to_end_research_run(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        loaded = _load_real_shape_snapshot(tmp_path)
        research_cfg = _research_execution_config()

        # Precondition: research surface must be ready under this config.
        readiness = check_execution_readiness(
            loaded,
            allow_provisional_fee_model=research_cfg.allow_provisional_fee_model,
            simulation_quantity_quantum=research_cfg.simulation_quantity_quantum,
        )
        assert readiness.research_simulation_readiness == "ready", (
            f"research surface unexpectedly unresolved: "
            f"{readiness.research_missing_requirements!r}"
        )

        # Build a short integer-OHLC dataset and a tiny 3-candle SMA
        # baseline (production numbers are 1_200; the test uses 3 for
        # deterministic hand-verified crossings).
        dataset = _build_integer_dataset()
        strategy = BaselineStrategyConfig(
            rule_id="price_over_sma",
            ma_type="SMA",
            lookback_candles=3,
            warmup_candles=3,
            unit_minutes=240,
            market="KRW-BTC",
        )
        backtest_cfg = BacktestConfig(
            starting_cash_krw=Money(Decimal("10_000_000")),
            target_sleeve_fraction=Decimal("1.0"),
            protective_stop_fraction=Decimal("0.10"),
            strategy=strategy,
            execution=research_cfg,
        )

        # Backtest runs without domain refusals.
        result = run_backtest(dataset, loaded, backtest_cfg)
        assert result.invalid_reason is None, (
            f"backtest refused: {result.refusal_code}={result.invalid_reason!r}"
        )
        # The 3-candle SMA fires a LONG cross at index 2 (close 104_000_000
        # vs mean 101_333_333.33), then a CASH cross a few candles later
        # (close 100_000_000 vs mean 103_000_000). Both transitions must
        # execute through the simulator — buy at candle index 3 open,
        # sell at candle index 5 open.
        sides = [entry.side for entry in result.entries]
        assert sides == ["buy", "sell"], sides
        assert result.used_provisional_fee_model is True

        buy_entry, sell_entry = result.entries
        # Tick came from the official schedule for prices in the
        # 50_000_000 KRW band → 1000-KRW tick. Fill prices must be
        # exact multiples of 1000 after slippage snap.
        assert buy_entry.fill_price.value % Decimal("1000") == 0
        assert sell_entry.fill_price.value % Decimal("1000") == 0
        # Buy adverse (round up): fill_price > candle open.
        assert buy_entry.fill_price.value > dataset.candles[3].open.value
        # Sell adverse (round down): fill_price <= candle open.
        assert sell_entry.fill_price.value <= dataset.candles[5].open.value

        # Research quantum is 1 satoshi; both filled quantities land on
        # a 1e-8 grid.
        assert (
            buy_entry.filled_qty.value % _RESEARCH_BTC_QUANTUM == 0
        ), buy_entry.filled_qty.value
        assert (
            sell_entry.filled_qty.value % _RESEARCH_BTC_QUANTUM == 0
        ), sell_entry.filled_qty.value

        # Evaluation runs and produces a valid, source-not-refused report.
        report = evaluate_backtest(dataset, loaded, backtest_cfg, result)
        assert report.source_run_refused is False
        assert report.evaluation_invalid_reason is None
        assert report.strategy_net_return is not None
        assert report.starting_equity_krw is not None
        assert report.closed_trade_count == 1  # one closed buy/sell pair
        assert report.position_entry_count == 1
        assert report.ledger_entry_count == 2

    def test_live_readiness_stays_unresolved_after_research_run(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        """A green research run MUST NOT be interpreted as live-ready.

        This assertion pins the invariant: the same loaded snapshot's
        ``live_execution_readiness`` must stay ``unresolved`` — enabling
        research simulation never promotes the live surface.
        """
        loaded = _load_real_shape_snapshot(tmp_path)
        report = check_execution_readiness(
            loaded,
            allow_provisional_fee_model=True,
            simulation_quantity_quantum=_RESEARCH_BTC_QUANTUM,
        )
        assert report.live_execution_readiness == "unresolved"
        # M6B blockers explicit.
        for expected in (
            "market_buy_fee_reservation",
            "default_step",
            "market_buy_price_support",
            "market_sell_market_support",
            "live_order_acceptance",
        ):
            assert expected in report.live_missing_requirements
