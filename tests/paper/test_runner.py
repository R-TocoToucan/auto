"""Hand-verified tests for :func:`bithumb_bot.paper.runner.run_paper_session`.

Covers hard-requirement items #1 (warmup excluded from forward P&L),
#2 (signals use only completed candles), and #3 (fill occurs no
earlier than the next candle).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.core.money import Money
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session

from .conftest import (
    STEP,
    flat,
    make_backtest_config,
    make_candle,
    make_dataset,
    make_snapshot,
    make_strategy_config,
)


class TestWarmupExcludedFromForwardPnl:
    def test_warmup_sourced_entry_excluded_from_forward_activity(
        self, tmp_path: Path
    ) -> None:
        # Warmup: 1199 flat candles, then the LAST warmup candle (index
        # 1199) spikes and crosses the hysteresis band — the earliest
        # index at which the strategy has enough lookback history to
        # emit a signal at all. This signal's source_open_time_utc is
        # still INSIDE the warmup slice (< paper_start_ts_utc).
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT - 1)]
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT - 1,
                "100000000",
                "200000000",
                "100000000",
                "200000000",
            )
        )
        # Forward: candle at paper_start (index 1200) is the fill candle
        # for the warmup-sourced BUY, and stays flat (no new signal).
        candles.append(flat(WARMUP_CANDLE_COUNT, "200000000"))
        # A genuine forward-sourced CASH transition (extreme drop) and
        # its fill candle.
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT + 1, "200000000", "200000000", "1", "1"
            )
        )
        candles.append(flat(WARMUP_CANDLE_COUNT + 2, "1"))

        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        assert result.paper_start_ts_utc == candles[WARMUP_CANDLE_COUNT].open_time_utc

        # The underlying (unfiltered) backtest sees BOTH the warmup-
        # sourced buy and the forward-sourced sell.
        assert result.backtest_result is not None
        assert len(result.backtest_result.entries) == 2
        assert result.backtest_result.entries[0].side == "buy"
        assert result.backtest_result.entries[1].side == "sell"

        # forward_entries excludes the warmup-sourced buy entirely.
        assert all(
            e.source_open_time_utc >= result.paper_start_ts_utc
            for e in result.forward_entries
        )
        assert len(result.forward_entries) == 1
        assert result.forward_entries[0].side == "sell"

        # forward_signal_count matches only the signal sourced at/after
        # paper_start_ts_utc (the CASH transition) — the warmup-sourced
        # LONG transition is not counted.
        assert result.forward_signal_count == 1


class TestSignalUsesOnlyCompletedCandles:
    def test_incomplete_final_candle_refuses_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(flat(WARMUP_CANDLE_COUNT, "100000000"))
        candles.append(flat(WARMUP_CANDLE_COUNT + 1, "100000000"))
        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()

        # now_utc equals the final candle's OPEN — its close boundary
        # (open + STEP) has not yet passed.
        now = dataset.candles[-1].open_time_utc

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.refusal_code == "IncompleteCandleError"
        assert result.invalid_reason is not None
        assert not (tmp_path / "state.json").exists()
        assert not (tmp_path / "fills.jsonl").exists()
        assert not (tmp_path / "signals.jsonl").exists()


class TestFillOccursOnNextCandle:
    def test_buy_and_sell_fill_on_the_candle_after_the_signal(
        self, tmp_path: Path
    ) -> None:
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        # index 1200 (paper_start): LONG transition.
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT,
                "100000000",
                "200000000",
                "100000000",
                "200000000",
            )
        )
        # index 1201: fill candle for the buy; no new signal.
        candles.append(flat(WARMUP_CANDLE_COUNT + 1, "200000000"))
        # index 1202: continuation, no transition.
        candles.append(flat(WARMUP_CANDLE_COUNT + 2, "200000000"))
        # index 1203: a decline that crosses the lower hysteresis band
        # (SMA-cross CASH transition) WITHOUT breaching the protective
        # stop — this test isolates "next-candle fill" from the
        # protective-stop path (covered separately in
        # tests/backtest/test_runner.py). `low=50_000_000` stays well
        # above the (deliberately very deep, see below) stop level.
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT + 3, "200000000", "200000000", "50000000", "50000000"
            )
        )
        # index 1204: fill candle for the sell.
        candles.append(flat(WARMUP_CANDLE_COUNT + 4, "50000000"))

        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        # protective_stop_fraction="0.99" -> stop is ~1% of the fill
        # price, far below every candle's low here, so the strategy's
        # own CASH signal (not a stop-out) drives the sell.
        config = BacktestConfig(
            starting_cash_krw=Money(Decimal("20000000")),
            target_sleeve_fraction=Decimal("1.0"),
            protective_stop_fraction=Decimal("0.99"),
            strategy=make_strategy_config(),
            execution=ExecutionConfig(
                slippage_bps_per_side=Decimal("50"),
                max_notional_krw=Money(Decimal("100000000")),
                allow_provisional_fee_model=False,
            ),
        )
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        assert len(result.forward_entries) == 2
        buy, sell = result.forward_entries
        assert buy.side == "buy"
        assert sell.side == "sell"

        # Fill happens strictly on the NEXT candle after the signal's
        # source candle — never the same candle.
        for entry in (buy, sell):
            assert entry.fill_ts_utc != entry.source_open_time_utc
            assert entry.fill_ts_utc == entry.source_open_time_utc + STEP

        assert buy.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 1].open_time_utc
        assert sell.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 4].open_time_utc
