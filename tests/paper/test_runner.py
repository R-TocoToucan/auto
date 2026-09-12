"""Hand-verified tests for :func:`bithumb_bot.paper.runner.run_paper_session`.

Covers the D-erv D1 warmup-isolation fix (a warmup-only transition
re-emits as a forward-only trade, never a phantom sell of an unopened
position), signals using only completed candles, and fills occurring
no earlier than the next candle.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.core.money import Money
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session
from bithumb_bot.paper.state import load_state

from .conftest import (
    STEP,
    flat,
    make_backtest_config,
    make_candle,
    make_dataset,
    make_snapshot,
    make_strategy_config,
)


class TestWarmupSignalDoesNotLeakIntoForward:
    """D-erv (D1): the fresh-portfolio forward loop never lets a
    warmup-only transition carry a filled position across
    ``paper_start_ts_utc``. See ``paper/runner.py``'s module docstring
    and the ``_forward_only_signals`` worked trace."""

    def test_warmup_sourced_long_becomes_forward_only_buy_then_sell(
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
        # Forward: candle at paper_start (index 1200) stays flat at the
        # spike level (no new signal there) — the paper session's OWN
        # fresh-CASH baseline diverges from the warmup-inherited LONG
        # state right here, so a SYNTHETIC forward LONG transition is
        # re-emitted at this candle (see module docstring). Index 1201
        # (also flat) is the buy's fill candle. Index 1202 is a genuine
        # forward-sourced CASH transition (a drop, but not so extreme
        # that its own low would breach the protective stop below —
        # see the config comment). Index 1203 is the sell's fill
        # candle.
        candles.append(flat(WARMUP_CANDLE_COUNT, "200000000"))
        candles.append(flat(WARMUP_CANDLE_COUNT + 1, "200000000"))
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT + 2, "200000000", "200000000", "90000000", "90000000"
            )
        )
        candles.append(flat(WARMUP_CANDLE_COUNT + 3, "90000000"))

        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        # protective_stop_fraction="0.60" -> stop sits at ~40% of the
        # buy's fill price (~80.4M), safely BELOW the drop candle's low
        # (90M) — so the forward SELL below is driven by the genuine
        # CASH signal, not a protective-stop trigger, isolating D1's
        # forward-only re-emission from the (separately-tested)
        # protective-stop path, matching
        # ``TestFillOccursOnNextCandle``'s convention.
        config = BacktestConfig(
            starting_cash_krw=Money(Decimal("20000000")),
            target_sleeve_fraction=Decimal("1.0"),
            protective_stop_fraction=Decimal("0.60"),
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
        assert result.paper_start_ts_utc == candles[WARMUP_CANDLE_COUNT].open_time_utc

        # D1: the warmup-sourced LONG state is re-emitted as a
        # FORWARD-only transition at paper_start (fresh CASH baseline),
        # producing a real BUY — NOT a phantom SELL of a
        # warmup-inherited position that was never opened in the
        # forward ledger.
        assert len(result.forward_entries) == 2
        buy, sell = result.forward_entries
        assert buy.side == "buy"
        assert sell.side == "sell"
        assert buy.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 1].open_time_utc
        assert sell.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 3].open_time_utc
        assert all(
            e.source_open_time_utc >= result.paper_start_ts_utc
            for e in result.forward_entries
        )

    def test_all_flat_dataset_yields_clean_paper_start(
        self,
        tmp_path: Path,
        flat_warmup_and_forward_dataset: tuple[
            CandleDataset, object, BacktestConfig
        ],
    ) -> None:
        dataset, snapshot, config = flat_warmup_and_forward_dataset
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        assert result.forward_entries == ()

        state = load_state(tmp_path)
        assert state is not None
        assert state.final_cash_krw == format(config.starting_cash_krw.value, "f")
        assert state.final_position_qty == "0"


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
