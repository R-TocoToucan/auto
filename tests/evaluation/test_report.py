"""Focused hand-verified tests for the post-hoc backtest evaluator.

Fixtures mirror ``tests/backtest/test_runner.py``: strategy uses
``lookback_candles = 3``; snapshot uses ``tick = 1`` so low-price
fixtures don't get snapped to a 1000-KRW multiple and blow up the
10% protective-stop hand math.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from bithumb_bot.backtest import BacktestConfig, run_backtest
from bithumb_bot.backtest.runner import BacktestResult
from bithumb_bot.bithumb_spec.snapshot import FeeRates, Minimums, SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.evaluation import (
    PERIODS_PER_YEAR,
    RISK_FREE_RATE,
    evaluate_backtest,
)
from bithumb_bot.execution import (
    ExecutionConfig,
    LedgerEntry,
    LedgerState,
)
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance
from bithumb_bot.strategy import BaselineStrategyConfig

UNIT = 240
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=UNIT)


# ---------------------------------------------------------------------------
# fixture builders (mirror the backtest tests)
# ---------------------------------------------------------------------------


def _c(
    idx: int,
    *,
    o: str,
    h: str | None = None,
    low: str | None = None,
    c: str,
) -> Candle:
    o_d = Decimal(o)
    c_d = Decimal(c)
    h_val = h if h is not None else str(max(o_d, c_d))
    l_val = low if low is not None else str(min(o_d, c_d))
    return Candle(
        market="KRW-BTC",
        unit_minutes=UNIT,
        open_time_utc=T0 + STEP * idx,
        open=o,
        high=h_val,
        low=l_val,
        close=c,
        volume="1",
        quote_volume=c,
    )


def _flat(idx: int, price: str) -> Candle:
    return _c(idx, o=price, h=price, low=price, c=price)


def _dataset(
    candles: list[Candle],
    *,
    missing: list[str] | None = None,
) -> CandleDataset:
    end = candles[-1].open_time_utc + STEP if candles else T0
    return CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=UNIT,
        requested_start_utc=(
            candles[0].open_time_utc.isoformat() if candles else T0.isoformat()
        ),
        requested_end_utc=end.isoformat(),
        fetched_at_utc="2026-09-09T00:00:00+00:00",
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


def _snapshot(
    *,
    buy_status: Literal[
        "confirmed_read_only",
        "provisional_documented",
        "unresolved_until_M6B",
        "contradicted",
    ] = "confirmed_read_only",
    general_status: Literal[
        "confirmed_read_only",
        "provisional_documented",
        "unresolved_until_M6B",
        "contradicted",
    ] = "confirmed_read_only",
    tick_present: bool = True,
) -> SnapshotV1:
    return SnapshotV1(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        retrieved_at_utc="2026-09-09T00:00:00Z",
        source_endpoints=["/v1/orders/chance"],
        fee_rates=FeeRates(bid="0.0025", ask="0.0025"),
        # Both minimums are KRW-denominated notionals; see
        # bithumb_bot.evaluation.report._hypothetical_liquidation for
        # the KRW-vs-KRW dust check.
        minimums=Minimums(krw_min_total_bid="5000", krw_min_total_ask="5000"),
        price_tick_rules=({"default_tick": Decimal("1")} if tick_present else {}),
        quantity_step_rules={"default_step": Decimal("0.001")},
        supported_order_types=["price", "market", "limit"],
        verification_status={
            "general_fee_rate": general_status,
            "market_buy_fee_reservation": buy_status,
            "rounding_rejection_behavior": "unresolved_until_M6B",
            "live_order_acceptance": "unresolved_until_M6B",
        },
        source_fixture_hashes=["0" * 64],
    )


def _cfg(
    *,
    cash: str = "20000000",
    lookback: int = 3,
    target_sleeve: str = "1.0",
    stop_frac: str = "0.10",
    max_notional_krw: str = "100000000",
    allow_provisional: bool = False,
) -> BacktestConfig:
    return BacktestConfig(
        starting_cash_krw=Money(Decimal(cash)),
        target_sleeve_fraction=Decimal(target_sleeve),
        protective_stop_fraction=Decimal(stop_frac),
        strategy=BaselineStrategyConfig(
            rule_id="price_over_sma",
            ma_type="SMA",
            lookback_candles=lookback,
            warmup_candles=lookback,
            unit_minutes=UNIT,
            market="KRW-BTC",
        ),
        execution=ExecutionConfig(
            slippage_bps_per_side=Decimal("50"),
            max_notional_krw=Money(Decimal(max_notional_krw)),
            allow_provisional_fee_model=allow_provisional,
        ),
    )


# ---------------------------------------------------------------------------
# 1. Flat cash-only curve
# ---------------------------------------------------------------------------


class TestFlatCashOnlyCurve:
    def test_no_trades_flat_prices_zero_return_zero_vol_none_sharpe(self) -> None:
        # All closes equal → SMA == close → CASH forever → no trades.
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.invalid_reason is None
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert report.evaluation_invalid_reason is None
        assert report.source_run_refused is False
        assert report.entries == ()
        # Post-warm-up curve: candles [2..5], 4 points.
        assert len(report.equity_curve) == 4
        # Cash stays flat at starting cash on every point.
        for point in report.equity_curve:
            assert point.cash_krw.value == Decimal("20000000")
            assert point.position_qty.value == Decimal("0")
            assert point.mark_to_market_equity_krw.value == Decimal("20000000")
            assert point.net_liquidation_equity_krw.value == Decimal("20000000")
        assert report.strategy_net_return == Decimal("0")
        assert report.total_actual_fees_krw.value == Decimal("0")
        assert report.total_modeled_slippage_krw.value == Decimal("0")
        assert report.max_drawdown_fraction == Decimal("0")
        # Zero-vol → Sharpe undefined.
        assert report.annualized_volatility == Decimal("0")
        assert report.annualized_sharpe is None
        # No closed trades.
        assert report.closed_trade_count == 0
        assert report.closed_trade_win_rate is None
        assert report.average_holding_period_hours is None


# ---------------------------------------------------------------------------
# 2. One profitable completed trade
# ---------------------------------------------------------------------------


class TestOneProfitableCompletedTrade:
    def test_buy_then_sell_higher_profitable_win_rate_one(self) -> None:
        # Force a buy at candle 3, a CASH transition at candle 5, sell at 6.
        # closes: [100,100,101,110,120,50,50]
        #   i=2 [100,100,101] sma=100.33 close=101 → LONG (txn) → BUY pending
        #   i=3 [100,101,110] sma=103.66 close=110 → LONG (no txn); BUY fills @ open 110
        #     fill_price = ceil(110*1.005, 1) = ceil(110.55, 1) = 111
        #     stop = 111 * 0.9 = 99.9
        #   i=4 [101,110,120] sma=110.33 close=120 → LONG (no txn); low=110>stop.
        #   i=5 [110,120,50]  sma=93.33  close=50  → CASH (txn) → SELL pending
        #                                             low=50 <= 99.9 → intrabar stop!
        #     Actually step 4 evaluates stop BEFORE step 6 sees the signal, so:
        #     stop triggers intrabar at candle 5 → SELL, lockout ON.
        #     Then step 6: CASH transition + position 0 + lockout → clear lockout.
        # So we get exactly one closed trade (buy + stop-out sell).
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "110"),
            _flat(4, "120"),
            _c(5, o="120", h="120", low="50", c="50"),
            _flat(6, "50"),
        ]
        # To avoid stop-out and test a strategy-signal SELL, use a rise-then-
        # slight-fall path so low > stop.
        # Simpler alternative: use prices that avoid the 10% drop.
        # Let me redesign:
        # closes: [100,100,101,102,103,101,100]
        #   i=2 [100,100,101] → LONG; BUY pending
        #   i=3 [100,101,102] sma=101 close=102 → LONG; BUY fills @102
        #     fill_price=ceil(102*1.005,1)=103; stop=103*0.9=92.7
        #   i=4 [101,102,103] sma=102 close=103 → LONG; low=103>92.7 ok
        #   i=5 [102,103,101] sma=102 close=101 → CASH (101<102) → SELL pending
        #   i=6 [103,101,100] sma≈101.33 close=100 → CASH; SELL fills @100
        #     fill_price=floor(100*0.995,1)=99. Position sold at 99.
        # buy at 103, sell at 99: PnL negative — this is a LOSING trade, not a win!
        # Redesign for win: buy low, sell high. But CASH transition requires close<SMA.
        # Structural challenge: the strategy exits on close<SMA, which almost by
        # definition means recent prices are below prior peak — hard to construct
        # a clean strategy-signal WIN. Let me use a specific path:
        # closes: [100,100,101,120,130,100,90]
        #   i=2 [100,100,101] → LONG; BUY
        #   i=3 [100,101,120] → LONG (120>107); BUY fills @120→121; stop=108.9
        #   i=4 [101,120,130] → LONG (130>117)
        #   i=5 [120,130,100] sma≈116.67 close=100 → CASH (txn); SELL pending
        #     low=100<108.9 → stop intrabar first → SELL at stop=108.9 (adverse)
        #     fill_price = floor(108.9*0.995, 1) = 108.
        # So it's a stop-out at 108 with buy at 121 → LOSS again.
        # For a WIN with intrabar stop path avoided, need low > stop throughout
        # the winning period, then a CASH signal while low > stop.
        # closes: [100,100,101,120,130,140,110]
        #   i=2 LONG BUY
        #   i=3 BUY fills @120 → fill_price ~121; stop ~108.9
        #   i=4 sma=117 close=130 LONG; low=130
        #   i=5 sma=125 close=140 LONG (140>125); low=140
        #   i=6 sma=133.33 close=110 CASH (110<133.33) → SELL pending
        #     low=110>108.9 → no stop trigger. But there's no candle 7 for SELL to fill.
        # Add candle 7: close=115, low=115. sma=125 close=115 CASH (no txn).
        #   SELL fills @candle_7.open=115 → fill_price=floor(115*0.995,1)=114.
        # Buy at 121, sell at 114 → LOSS again due to slippage/tick eating margin.
        # Bigger price gap: buy at ~100, sell at ~200.
        # closes: [100,100,101,105,200,100,150,150]
        #   i=2 LONG BUY
        #   i=3 [100,101,105] sma=102 close=105 LONG; BUY fills @105 → 106; stop=95.4
        #   i=4 [101,105,200] sma=135.33 close=200 LONG
        #   i=5 [105,200,100] sma=135 close=100 CASH → SELL pending
        #     low=100 > stop=95.4 → no stop trigger.
        #     But wait: SELL fills at NEXT candle, i=6.
        #   i=6 [200,100,150] sma=150 close=150 → equality → CASH (no txn)
        #     but SELL fills @candle6.open=150 → sell fill_price=floor(150*0.995,1)=149
        # Buy at 106, sell at 149: WIN.
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "105"),
            _flat(4, "200"),
            _c(5, o="100", h="100", low="100", c="100"),  # avoid stop trigger
            _flat(6, "150"),
            _flat(7, "150"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.invalid_reason is None
        # Confirm we got exactly one buy + one strategy sell.
        assert len(result.entries) == 2
        buy, sell = result.entries
        assert buy.side == "buy" and sell.side == "sell"
        assert sell.execution_reason == "strategy_signal"

        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert report.evaluation_invalid_reason is None
        assert report.closed_trade_count == 1
        assert report.closed_trade_win_rate == Decimal("1")
        # net_return should be positive.
        assert report.strategy_net_return is not None
        assert report.strategy_net_return > Decimal("0")


# ---------------------------------------------------------------------------
# 3. One losing completed trade
# ---------------------------------------------------------------------------


class TestOneLosingCompletedTrade:
    def test_stop_out_losing_win_rate_zero(self) -> None:
        # Force a buy then a same-later-candle intrabar stop-out.
        # closes: [100M,100M,101M,101M(low 80M),80M,80M,80M]
        #   i=2 LONG (BUY pending)
        #   i=3 BUY fills @101M → 101_505_000; stop=91_354_500
        #     low=80M <= stop → intrabar SELL (adverse) at stop
        #   Lockout ON. Then candle 4 CASH (80M < sma≈95.66M) → clears lockout.
        c3 = _c(3, o="101000000", h="101000000", low="80000000", c="101000000")
        candles = [
            _flat(0, "100000000"),
            _flat(1, "100000000"),
            _flat(2, "101000000"),
            c3,
            _flat(4, "80000000"),
            _flat(5, "80000000"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.invalid_reason is None
        assert len(result.entries) == 2
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert report.closed_trade_count == 1
        assert report.closed_trade_win_rate == Decimal("0")
        assert report.strategy_net_return is not None
        assert report.strategy_net_return < Decimal("0")


# ---------------------------------------------------------------------------
# 4. Open position at dataset end
# ---------------------------------------------------------------------------


class TestOpenPositionAtDatasetEnd:
    def test_open_position_excluded_from_win_rate_included_in_liquidation(
        self,
    ) -> None:
        # BUY fills but no CASH signal before end.
        # closes: [100,100,101,102,103,104]  (all LONG post-warmup)
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "102"),
            _flat(4, "103"),
            _flat(5, "104"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.invalid_reason is None
        assert len(result.entries) == 1
        assert result.final_position_qty.value > 0
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert report.closed_trade_count == 0
        assert report.closed_trade_win_rate is None
        assert report.average_holding_period_hours is None
        assert report.open_position_qty.value > 0
        # Ending net_liq reflects the hypothetical sell.
        assert report.ending_net_liquidation_equity_krw is not None
        assert report.estimated_final_liquidation_fee_krw.value > 0


# ---------------------------------------------------------------------------
# 5. Actual fees counted once
# ---------------------------------------------------------------------------


class TestActualFeesCountedOnce:
    def test_total_actual_fees_equals_sum_of_ledger_fees(self) -> None:
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "105"),
            _flat(4, "200"),
            _c(5, o="100", h="100", low="100", c="100"),
            _flat(6, "150"),
            _flat(7, "150"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.invalid_reason is None
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        expected = sum((e.fee_krw.value for e in result.entries), Decimal("0"))
        assert report.total_actual_fees_krw.value == expected


# ---------------------------------------------------------------------------
# 6. Modeled slippage counted once
# ---------------------------------------------------------------------------


class TestModeledSlippageCountedOnce:
    def test_slippage_matches_hand_math(self) -> None:
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "105"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.invalid_reason is None
        assert len(result.entries) == 1
        buy = result.entries[0]
        # BUY fill: candle 3 open = 105; slippage 50bps → 105*1.005=105.525;
        # ceil to tick=1 → 106. slippage_per_unit = 106 - 105 = 1.
        # Modeled slippage = filled_qty * 1.
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        expected = buy.filled_qty.value * (Decimal("106") - Decimal("105"))
        assert report.total_modeled_slippage_krw.value == expected


# ---------------------------------------------------------------------------
# 7. Hypothetical liquidation does not mutate the ledger
# ---------------------------------------------------------------------------


class TestHypotheticalLiquidationDoesNotMutate:
    def test_backtest_result_ledger_length_unchanged(self) -> None:
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "102"),
            _flat(4, "103"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        entries_before = tuple(result.entries)
        n_before = len(entries_before)
        cash_before = result.final_cash_krw
        pos_before = result.final_position_qty
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        # Ledger unchanged.
        assert result.entries == entries_before
        assert len(result.entries) == n_before
        assert result.final_cash_krw == cash_before
        assert result.final_position_qty == pos_before
        # Report has liquidation fee separate from actual fees.
        assert report.total_actual_fees_krw.value == sum(
            (e.fee_krw.value for e in entries_before), Decimal("0")
        )
        assert report.estimated_final_liquidation_fee_krw.value > 0
        # Sanity: the estimated liquidation fee is NOT included in total_actual_fees.
        assert (
            report.estimated_final_liquidation_fee_krw.value
            not in {report.total_actual_fees_krw.value}
        )


# ---------------------------------------------------------------------------
# 8. Maximum drawdown
# ---------------------------------------------------------------------------


class TestMaximumDrawdown:
    def test_drawdown_from_hand_constructed_equity_series(self) -> None:
        # Flat cash → no drawdown.
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg()
        report = evaluate_backtest(
            _dataset(candles),
            _snapshot(),
            cfg,
            run_backtest(_dataset(candles), _snapshot(), cfg),
        )
        assert report.max_drawdown_fraction == Decimal("0")

    def test_drawdown_positive_when_position_value_falls(self) -> None:
        # Buy near a peak, then a drop → drawdown > 0.
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "150"),
            _flat(4, "160"),
            _flat(5, "155"),  # slight drop from peak
            _flat(6, "155"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert report.max_drawdown_fraction is not None
        assert report.max_drawdown_fraction > Decimal("0")
        assert report.max_drawdown_fraction <= Decimal("1")


# ---------------------------------------------------------------------------
# 9. Zero-volatility Sharpe returns None
# ---------------------------------------------------------------------------


class TestZeroVolSharpeIsNone:
    def test_flat_curve_zero_vol_none_sharpe(self) -> None:
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg()
        snapshot = _snapshot()
        report = evaluate_backtest(
            _dataset(candles),
            snapshot,
            cfg,
            run_backtest(_dataset(candles), snapshot, cfg),
        )
        assert report.annualized_volatility == Decimal("0")
        assert report.annualized_sharpe is None


# ---------------------------------------------------------------------------
# 10. Aligned benchmark dates and costs
# ---------------------------------------------------------------------------


class TestAlignedBenchmark:
    def test_benchmark_shares_first_and_final_timestamps(self) -> None:
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg()
        snapshot = _snapshot()
        report = evaluate_backtest(
            _dataset(candles),
            snapshot,
            cfg,
            run_backtest(_dataset(candles), snapshot, cfg),
        )
        # Evaluation window: candle W=2 open → candle 5 open.
        assert report.evaluation_first_open_utc == T0 + STEP * 2
        assert report.evaluation_last_open_utc == T0 + STEP * 5
        # Benchmark valid on this flat data.
        assert report.benchmark_net_return is not None
        # Buy-and-hold at flat price yields negative return (costs only).
        assert report.benchmark_net_return < Decimal("0")

    def test_benchmark_refused_on_cap_violation(self) -> None:
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg(cash="20000000", max_notional_krw="10000000")
        snapshot = _snapshot()
        # Backtest fails closed on cap (LONG signal never fires since
        # closes are flat → equality → CASH). Actually flat → no LONG,
        # cap never hit. Force a LONG scenario to hit it.
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),  # LONG txn — sizing exceeds 10M cap
            _flat(3, "101"),
        ]
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.invalid_reason is not None
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        # Source is invalid → benchmark invalid too, all metrics None.
        assert report.source_run_refused is True
        assert report.strategy_net_return is None
        assert report.benchmark_net_return is None
        assert report.strategy_minus_benchmark_net_return is None
        assert report.benchmark_invalid_reason is not None


# ---------------------------------------------------------------------------
# 11. Invalid BacktestResult remains invalid
# ---------------------------------------------------------------------------


class TestInvalidBacktestRemainsInvalid:
    def test_invalid_source_headline_metrics_all_none(self) -> None:
        # Trigger source refusal via cap breach on a LONG signal.
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "101"),
        ]
        cfg = _cfg(cash="20000000", max_notional_krw="10000000")
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.invalid_reason is not None
        assert result.refusal_code == "NotionalCapExceededError"

        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        # Source refusal + code preserved.
        assert report.source_invalid_reason == result.invalid_reason
        assert report.source_refusal_code == result.refusal_code
        assert report.source_run_refused is True
        assert report.refused_run_count == 1
        # Headline metrics all None.
        for name in (
            "strategy_net_return",
            "fee_addback_return",
            "max_drawdown_fraction",
            "annualized_volatility",
            "annualized_sharpe",
            "closed_trade_win_rate",
            "turnover",
            "average_holding_period_hours",
            "benchmark_net_return",
            "strategy_minus_benchmark_net_return",
            "starting_equity_krw",
            "ending_mark_to_market_equity_krw",
            "ending_net_liquidation_equity_krw",
        ):
            assert getattr(report, name) is None, f"{name} must be None on invalid source"
        # Diagnostic accounting still populated.
        assert report.total_actual_fees_krw.value >= Decimal("0")
        assert report.closed_trade_count >= 0
        assert report.ledger_entry_count == len(result.entries)
        assert report.position_entry_count == sum(
            1 for e in result.entries if e.side == "buy"
        )

    def test_insufficient_candles_after_warmup_is_invalid(self) -> None:
        # lookback=3 → need >= 4 candles. Give 3 → invalid.
        candles = [_flat(i, "100") for i in range(3)]
        cfg = _cfg(lookback=3)
        snapshot = _snapshot()
        report = evaluate_backtest(
            _dataset(candles),
            snapshot,
            cfg,
            run_backtest(_dataset(candles), snapshot, cfg),
        )
        assert report.evaluation_invalid_reason is not None
        assert report.evaluation_refusal_code == "InsufficientCandlesError"
        assert report.strategy_net_return is None
        assert report.benchmark_net_return is None

    def test_unverified_sell_fee_is_evaluation_invalid(self) -> None:
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg()
        # general_fee_rate → unresolved → sell fee unverified.
        snapshot = _snapshot(general_status="unresolved_until_M6B")
        result = run_backtest(_dataset(candles), snapshot, cfg)
        # Backtest itself is fine (no sells fired on flat data).
        assert result.invalid_reason is None
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert report.evaluation_invalid_reason is not None
        assert report.evaluation_refusal_code == "UnverifiedFeeModelError"

    def test_missing_tick_is_evaluation_invalid(self) -> None:
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg()
        snapshot = _snapshot(tick_present=False)
        result = run_backtest(_dataset(candles), snapshot, cfg)
        # Runner didn't need tick (no fills on flat data) → valid.
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert report.evaluation_invalid_reason is not None
        assert report.evaluation_refusal_code == "SnapshotValidationError"


# ---------------------------------------------------------------------------
# 12. Final pending intent count
# ---------------------------------------------------------------------------


class TestPendingIntentCount:
    def test_pending_intent_count_zero_when_no_pending(self) -> None:
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg()
        snapshot = _snapshot()
        report = evaluate_backtest(
            _dataset(candles),
            snapshot,
            cfg,
            run_backtest(_dataset(candles), snapshot, cfg),
        )
        assert report.pending_intent_count == 0

    def test_pending_intent_count_one_on_last_candle_signal(self) -> None:
        # LONG transition on the last candle → pending intent.
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101")]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        assert result.pending_intent is not None
        # Report should be invalid (insufficient candles after warm-up)
        # but pending_intent_count is still populated from the ledger.
        report = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert report.pending_intent_count == 1


# ---------------------------------------------------------------------------
# 13. Deterministic evaluation
# ---------------------------------------------------------------------------


class TestDeterministicEvaluation:
    def test_two_calls_identical_output(self) -> None:
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "105"),
            _flat(4, "200"),
            _c(5, o="100", h="100", low="100", c="100"),
            _flat(6, "150"),
            _flat(7, "150"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        result = run_backtest(_dataset(candles), snapshot, cfg)
        a = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        b = evaluate_backtest(_dataset(candles), snapshot, cfg, result)
        assert a.equity_curve == b.equity_curve
        assert a.strategy_net_return == b.strategy_net_return
        assert a.benchmark_net_return == b.benchmark_net_return
        assert a.max_drawdown_fraction == b.max_drawdown_fraction
        assert a.annualized_volatility == b.annualized_volatility
        assert a.annualized_sharpe == b.annualized_sharpe


# ---------------------------------------------------------------------------
# 14. Appending candles outside processed range cannot change existing report
# ---------------------------------------------------------------------------


class TestNoLookAheadOnEvaluation:
    def test_appending_future_candles_preserves_prior_report(self) -> None:
        # Base run + report on N candles.
        base_candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "105"),
            _flat(4, "200"),
            _c(5, o="100", h="100", low="100", c="100"),
            _flat(6, "150"),
            _flat(7, "150"),
        ]
        cfg = _cfg()
        snapshot = _snapshot()
        base_result = run_backtest(_dataset(base_candles), snapshot, cfg)
        base_report = evaluate_backtest(
            _dataset(base_candles), snapshot, cfg, base_result
        )

        # Now append future candles. Re-run backtest + evaluation over
        # extended range. Prior post-warm-up equity points (up to the
        # base final candle open time) must be byte-equal in the new
        # report.
        extended = base_candles + [
            _flat(8, "160"),
            _flat(9, "170"),
        ]
        extended_result = run_backtest(_dataset(extended), snapshot, cfg)
        extended_report = evaluate_backtest(
            _dataset(extended), snapshot, cfg, extended_result
        )

        base_end_ts = base_candles[-1].open_time_utc
        base_prefix = tuple(
            p for p in base_report.equity_curve if p.candle_open_time_utc <= base_end_ts
        )
        extended_prefix = tuple(
            p
            for p in extended_report.equity_curve
            if p.candle_open_time_utc <= base_end_ts
        )
        assert base_prefix == extended_prefix

        # Also entries produced up through base range are byte-equal.
        base_entries_by_ts = {e.fill_ts_utc for e in base_result.entries}
        extended_entries_in_base_range = tuple(
            e for e in extended_result.entries if e.fill_ts_utc in base_entries_by_ts
        )
        assert extended_entries_in_base_range == base_result.entries


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_periods_per_year_is_2190(self) -> None:
        assert PERIODS_PER_YEAR == 6 * 365 == 2_190

    def test_risk_free_rate_is_zero(self) -> None:
        assert RISK_FREE_RATE == Decimal("0")


# ---------------------------------------------------------------------------
# Ledger consistency validation (corrupted ledger → invalid)
# ---------------------------------------------------------------------------


class TestLedgerConsistencyValidation:
    def _corrupted_result(self, entries: tuple[LedgerEntry, ...]) -> BacktestResult:
        # Build a BacktestResult by hand, bypassing the runner.
        return BacktestResult(
            final_state=LedgerState(
                cash_krw=Money(Decimal("20000000")),
                position_qty=Qty(Decimal("0")),
                entries=entries,
            ),
            entries=entries,
            final_cash_krw=Money(Decimal("20000000")),
            final_position_qty=Qty(Decimal("0")),
            active_protective_stop=None,
            pending_intent=None,
            stopped_out_lockout=False,
            processed_first_open_utc=T0,
            processed_last_open_utc=T0 + STEP * 5,
            invalid_reason=None,
            refusal_code=None,
            used_provisional_fee_model=False,
            signals_generated=0,
        )

    def test_orphan_sell_flagged(self) -> None:
        # A sell entry with no preceding buy.
        sell = LedgerEntry(
            side="sell",
            source_open_time_utc=T0,
            signal_ts_utc=T0 + STEP,
            fill_ts_utc=T0 + STEP,
            fill_price=Money(Decimal("100")),
            requested_notional_krw=Money(Decimal("0")),
            requested_qty=Qty(Decimal("0.1")),
            order_notional_krw=Money(Decimal("10")),
            filled_qty=Qty(Decimal("0.1")),
            net_acquired_coin=Qty(Decimal("0")),
            gross_proceeds_krw=Money(Decimal("10")),
            net_proceeds_krw=Money(Decimal("9.975")),
            fee_krw=Money(Decimal("0.025")),
            total_cash_debit_krw=Money(Decimal("0")),
            cash_before_krw=Money(Decimal("20000000")),
            cash_after_krw=Money(Decimal("20000009.975")),
            position_before_qty=Qty(Decimal("0.1")),
            position_after_qty=Qty(Decimal("0")),
        )
        candles = [_flat(i, "100") for i in range(6)]
        cfg = _cfg()
        snapshot = _snapshot()
        report = evaluate_backtest(
            _dataset(candles), snapshot, cfg, self._corrupted_result((sell,))
        )
        assert report.evaluation_invalid_reason is not None
        assert report.evaluation_refusal_code == "LedgerConsistencyError"
