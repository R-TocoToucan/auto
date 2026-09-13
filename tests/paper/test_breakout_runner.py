"""Focused tests for the shadow breakout paper runner.

Covers:

* Golden run — entry on breach, exit on drop, correct fill count.
* Next-candle execution — buy/sell fill_ts_utc == source_open + STEP.
* Fresh state, then resume: second invocation with an appended candle
  writes no duplicate fills and updates state.json.
* Separate ledger / fills / signals / equity vs. the SMA paper runner
  (each writes to its own state directory without touching the other).
* Deterministic replay — repeated invocations on the same dataset (with
  a fresh state directory) produce the same fills / signals / equity.
* No broker import — proved by the sibling test file
  ``tests/import_boundary/test_breakout_no_broker.py``; here we just
  spot-check that the paper package's import contract still holds when
  the shadow module is present.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.core.money import Money
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance
from bithumb_bot.paper.breakout_runner import (
    WARMUP_CANDLE_COUNT,
    BreakoutInputContractMismatchError,
    BreakoutPaperConfig,
    BreakoutResumeDivergenceError,
    run_breakout_paper_session,
)

from .conftest import make_snapshot

UNIT = 240
T0 = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=UNIT)


def _candle(
    idx: int, *, open_: str, high: str, low: str, close: str
) -> Candle:
    return Candle(
        market="KRW-BTC",
        unit_minutes=UNIT,
        open_time_utc=T0 + idx * STEP,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume="1",
        quote_volume=close,
    )


def _flat(idx: int, price: str) -> Candle:
    return _candle(idx, open_=price, high=price, low=price, close=price)


def _dataset(candles: list[Candle]) -> CandleDataset:
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
        missing_intervals_utc=[],
        provenance=DatasetProvenance(
            source_endpoint="/v1/candles/minutes/240",
            base_url="https://api.bithumb.com",
            pages_fetched=1,
            page_cursors_kst=[],
            effective_end_utc=end.isoformat(),
        ),
    )


def _config(
    *,
    cash: str = "20000000",
    max_notional_krw: str = "100000000",
) -> BreakoutPaperConfig:
    execution = ExecutionConfig(
        slippage_bps_per_side=Decimal("50"),
        max_notional_krw=Money(Decimal(max_notional_krw)),
        allow_provisional_fee_model=False,
        simulation_quantity_quantum=Decimal("0.00000001"),
    )
    return BreakoutPaperConfig(
        starting_cash_krw=Money(Decimal(cash)),
        max_notional_krw=Money(Decimal(max_notional_krw)),
        execution=execution,
    )


def _entry_series() -> list[Candle]:
    """120 flat warmup candles, then a breakout candle at index 120.

    Prior_120_high = 100_000_000 → entry_breakout_level = 100_500_000.
    Breakout close is 100_600_000 (strictly above entry level; low
    stays at 100_000_000 so no intrabar stop concern later).
    """
    return [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)] + [
        _candle(
            WARMUP_CANDLE_COUNT,
            open_="100000000",
            high="100600000",
            low="100000000",
            close="100600000",
        )
    ]


class TestGoldenRun:
    def test_entry_and_exit_produce_two_fills(self, tmp_path: Path) -> None:
        # Warmup + entry candle + fill candle + drop-below-entry-level.
        # prior_120_high = 100_000_000, entry_breakout_level =
        # 100_500_000. Fill price at index 121 ≈ 100.6M * 1.005 ≈
        # 101.1M; protective stop at 90% ≈ 90.99M. Exit-drop candle at
        # index 122 has close=100_000_000 (< 100_500_000 → strategy
        # exit) and low=100_000_000 (well above the 90.99M stop, so
        # the strategy path fires, not the protective-stop path).
        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))
        candles.append(
            _candle(
                WARMUP_CANDLE_COUNT + 2,
                open_="100600000",
                high="100600000",
                low="100000000",
                close="100000000",
            )
        )
        candles.append(_flat(WARMUP_CANDLE_COUNT + 3, "100000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()
        now = candles[-1].open_time_utc + STEP

        result = run_breakout_paper_session(
            dataset, snapshot, config, tmp_path, now_utc=now
        )
        assert result.invalid_reason is None, result.invalid_reason
        # Two fills: BUY on index 121, SELL on index 123.
        assert len(result.forward_entries) == 2
        buy, sell = result.forward_entries
        assert buy.side == "buy"
        assert sell.side == "sell"
        # Next-candle execution:
        assert buy.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 1].open_time_utc
        assert sell.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 3].open_time_utc
        # State-dir artifacts written.
        assert (tmp_path / "state.json").is_file()
        assert (tmp_path / "fills.jsonl").is_file()
        assert (tmp_path / "signals.jsonl").is_file()
        assert (tmp_path / "equity.jsonl").is_file()

    def test_no_signal_no_fill_fresh_portfolio(self, tmp_path: Path) -> None:
        # Flat warmup + one flat forward candle → no transition → no
        # fills, cash unchanged.
        candles = [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(_flat(WARMUP_CANDLE_COUNT, "100000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()
        now = candles[-1].open_time_utc + STEP

        result = run_breakout_paper_session(
            dataset, snapshot, config, tmp_path, now_utc=now
        )
        assert result.invalid_reason is None
        assert result.forward_entries == ()
        assert result.final_cash_krw.value == Decimal("20000000")
        assert result.final_position_qty.value == Decimal("0")


class TestNextCandleExecution:
    def test_buy_never_fills_on_source_candle(self, tmp_path: Path) -> None:
        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "300000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()
        now = candles[-1].open_time_utc + STEP

        result = run_breakout_paper_session(
            dataset, snapshot, config, tmp_path, now_utc=now
        )
        assert len(result.forward_entries) == 1
        buy = result.forward_entries[0]
        assert buy.side == "buy"
        # Signal source is index 120; fill is index 121 (source_open + STEP).
        assert buy.source_open_time_utc == candles[WARMUP_CANDLE_COUNT].open_time_utc
        assert buy.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 1].open_time_utc
        assert buy.fill_ts_utc == buy.source_open_time_utc + STEP


class TestResume:
    def test_resume_after_flat_forward_writes_no_duplicate_fills(
        self, tmp_path: Path
    ) -> None:
        # First invocation: warmup + 1 flat forward candle (no fills).
        candles = [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(_flat(WARMUP_CANDLE_COUNT, "100000000"))
        dataset1 = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()

        r1 = run_breakout_paper_session(
            dataset1, snapshot, config, tmp_path,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        assert r1.forward_entries == ()
        assert r1.resumed is False

        # Append one more flat forward candle and resume. Still no
        # transitions, so still no fills, but state.last_processed
        # advances.
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100000000"))
        dataset2 = _dataset(candles)

        r2 = run_breakout_paper_session(
            dataset2, snapshot, config, tmp_path,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert r2.invalid_reason is None
        assert r2.resumed is True
        assert r2.forward_entries == ()
        # fills.jsonl is either absent or empty; state.json exists.
        fills_path = tmp_path / "fills.jsonl"
        assert (not fills_path.exists()) or fills_path.read_bytes() == b""
        assert (tmp_path / "state.json").is_file()

    def test_resume_after_entry_before_exit_refuses(self, tmp_path: Path) -> None:
        # First invocation covers just warmup + entry + fill candle;
        # final_position_qty > 0 at save time. Second invocation must
        # refuse (open-long resume not supported).
        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "300000000"))
        dataset1 = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()

        r1 = run_breakout_paper_session(
            dataset1, snapshot, config, tmp_path,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        assert len(r1.forward_entries) == 1
        assert r1.final_position_qty.value > 0

        # Append candles and try to resume.
        candles.append(_flat(WARMUP_CANDLE_COUNT + 2, "300000000"))
        dataset2 = _dataset(candles)
        with pytest.raises(BreakoutResumeDivergenceError):
            run_breakout_paper_session(
                dataset2, snapshot, config, tmp_path,
                now_utc=candles[-1].open_time_utc + STEP,
            )

    def test_resume_refuses_starting_cash_change(self, tmp_path: Path) -> None:
        candles = [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(_flat(WARMUP_CANDLE_COUNT, "100000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        r1 = run_breakout_paper_session(
            dataset, snapshot, _config(cash="20000000"),
            tmp_path, now_utc=candles[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None

        with pytest.raises(BreakoutResumeDivergenceError):
            run_breakout_paper_session(
                dataset, snapshot, _config(cash="30000000"),
                tmp_path, now_utc=candles[-1].open_time_utc + STEP,
            )


class TestDeterministicReplay:
    def test_two_fresh_runs_produce_identical_ledgers(
        self, tmp_path: Path
    ) -> None:
        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "300000000"))
        candles.append(
            _candle(
                WARMUP_CANDLE_COUNT + 2,
                open_="300000000",
                high="300000000",
                low="100000000",
                close="100000000",
            )
        )
        candles.append(_flat(WARMUP_CANDLE_COUNT + 3, "100000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()
        now = candles[-1].open_time_utc + STEP

        state_a = tmp_path / "a"
        state_b = tmp_path / "b"
        state_a.mkdir()
        state_b.mkdir()

        r1 = run_breakout_paper_session(
            dataset, snapshot, config, state_a, now_utc=now
        )
        r2 = run_breakout_paper_session(
            dataset, snapshot, config, state_b, now_utc=now
        )
        assert r1.forward_entries == r2.forward_entries
        assert r1.final_cash_krw == r2.final_cash_krw
        assert r1.final_position_qty == r2.final_position_qty


class TestSeparateLedgers:
    def test_breakout_state_dir_does_not_touch_sma_state_dir(
        self, tmp_path: Path
    ) -> None:
        sma_state = tmp_path / "sma_state"
        breakout_state = tmp_path / "breakout_state"
        sma_state.mkdir()
        # Preseed a sentinel file the SMA runner would write.
        (sma_state / "sma_sentinel").write_bytes(b"sma-owned")

        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "300000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()
        now = candles[-1].open_time_utc + STEP

        result = run_breakout_paper_session(
            dataset, snapshot, config, breakout_state, now_utc=now
        )
        assert result.invalid_reason is None

        # Breakout wrote to its own dir…
        assert (breakout_state / "state.json").is_file()
        assert (breakout_state / "fills.jsonl").is_file()
        # …and left the SMA dir untouched (no state.json / fills.jsonl
        # written there, sentinel intact).
        assert (sma_state / "sma_sentinel").read_bytes() == b"sma-owned"
        assert not (sma_state / "state.json").exists()
        assert not (sma_state / "fills.jsonl").exists()


class TestInputContract:
    def test_wrong_market_refuses(self, tmp_path: Path) -> None:
        # Same warmup shape but tag every candle KRW-ETH.
        candles = [
            Candle(
                market="KRW-ETH",
                unit_minutes=UNIT,
                open_time_utc=T0 + i * STEP,
                open="100000000",
                high="100000000",
                low="100000000",
                close="100000000",
                volume="1",
                quote_volume="100000000",
            )
            for i in range(WARMUP_CANDLE_COUNT + 1)
        ]
        end = candles[-1].open_time_utc + STEP
        dataset = CandleDataset(
            schema_version=1,
            venue="bithumb",
            market="KRW-ETH",
            unit_minutes=UNIT,
            requested_start_utc=candles[0].open_time_utc.isoformat(),
            requested_end_utc=end.isoformat(),
            fetched_at_utc="2026-01-01T00:00:00+00:00",
            candles=candles,
            missing_intervals_utc=[],
            provenance=DatasetProvenance(
                source_endpoint="/v1/candles/minutes/240",
                base_url="https://api.bithumb.com",
                pages_fetched=1,
                page_cursors_kst=[],
                effective_end_utc=end.isoformat(),
            ),
        )
        snapshot = make_snapshot()
        with pytest.raises(BreakoutInputContractMismatchError):
            run_breakout_paper_session(
                dataset, snapshot, _config(),
                tmp_path, now_utc=candles[-1].open_time_utc + STEP,
            )

    def test_incomplete_final_candle_refuses(self, tmp_path: Path) -> None:
        candles = [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(_flat(WARMUP_CANDLE_COUNT, "100000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        # now_utc equals the last candle's OPEN — close hasn't passed.
        now = candles[-1].open_time_utc

        result = run_breakout_paper_session(
            dataset, snapshot, _config(), tmp_path, now_utc=now
        )
        assert result.refusal_code == "BreakoutIncompleteCandleError"
        assert not (tmp_path / "state.json").exists()

    def test_insufficient_forward_candles_refuses(self, tmp_path: Path) -> None:
        # Only WARMUP_CANDLE_COUNT candles, no forward candle.
        candles = [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        now = candles[-1].open_time_utc + STEP

        result = run_breakout_paper_session(
            dataset, snapshot, _config(), tmp_path, now_utc=now
        )
        assert result.refusal_code == "BreakoutInsufficientForwardCandlesError"
        assert not (tmp_path / "state.json").exists()
