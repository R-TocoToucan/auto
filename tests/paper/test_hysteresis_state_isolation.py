"""D-no0 D1 regression: the hysteresis strategy's internal current_state
variable never leaks across paper_start_ts_utc.

Exact reproducer from the task brief:
  - 1199 warmup candles at close=100_000_000
  - 1 warmup candle at close=101_000_000 (would trip strategy to LONG in
    warmup, at reduced-dataset index -1 = original 1199)
  - 1 forward candle at close=100_500_000
Under the reduced-dataset approach the SMA at the first forward candle is
(100_000_000*1198 + 101_000_000 + 100_500_000) / 1200 = 100_001_250,
upper hysteresis boundary at bps=75 is 100_001_250 * 10075 / 10000 =
100_751_259.375, close 100_500_000 < upper -> strategy stays CASH -> no
signal -> no fill -> fresh portfolio.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session
from bithumb_bot.paper.state import load_state

from .conftest import STEP, flat, make_backtest_config, make_candle, make_dataset, make_snapshot


class TestHysteresisStateIsolation:
    """D-no0 D1: a warmup-only spike that would have tripped the strategy
    to LONG had trading started earlier produces ZERO forward portfolio
    effect once the reduced-dataset fix is in place — the strategy's
    ``current_state`` at ``paper_start_ts_utc`` is unambiguously CASH by
    construction, not by re-derivation."""

    def test_no_signal_no_fill_fresh_portfolio(self, tmp_path: Path) -> None:
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT - 1)]
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT - 1,
                "101000000",
                "101000000",
                "101000000",
                "101000000",
            )
        )
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT,
                "100500000",
                "100500000",
                "100500000",
                "100500000",
            )
        )
        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()
        now = candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        assert result.forward_entries == ()
        assert result.forward_signal_count == 0
        assert result.new_fills_this_invocation == 0
        assert result.paper_start_ts_utc == candles[WARMUP_CANDLE_COUNT].open_time_utc

        state = load_state(tmp_path)
        assert state is not None
        assert state.paper_start_cash_krw == format(config.starting_cash_krw.value, "f")
        assert state.paper_start_position_qty == "0"
        assert state.paper_start_realized_pnl_krw == "0"
        assert state.paper_start_cumulative_fees_krw == "0"
        assert state.final_cash_krw == state.paper_start_cash_krw
        assert state.final_position_qty == "0"
        assert state.forward_candle_count == 1
        assert state.forward_fill_count == 0
        assert state.forward_signal_count == 0

        fills_path = tmp_path / "fills.jsonl"
        signals_path = tmp_path / "signals.jsonl"
        assert (not fills_path.exists()) or fills_path.read_bytes() == b""
        assert (not signals_path.exists()) or signals_path.read_bytes() == b""

    def test_arithmetic_matches_task_brief(self) -> None:
        closes = [Decimal("100000000")] * 1198 + [
            Decimal("101000000"),
            Decimal("100500000"),
        ]
        lookback = Decimal(1200)
        sma = sum(closes, Decimal("0")) / lookback
        assert sma == Decimal("100001250")

        upper = sma * (Decimal(10000) + Decimal(75)) / Decimal(10000)
        assert upper == Decimal("100751259.375")
        assert Decimal("100500000") < upper
