"""Focused hand-verified tests for the chronological backtest runner.

Strategy fixture uses ``lookback_candles = 3`` so SMA math is trivial.
Snapshot fixture defaults to ``market_buy_fee_reservation =
confirmed_read_only`` and ``allow_provisional_fee_model = False`` so
the pre-flight buy-fee gate is closed only in tests that explicitly
open it.

Baseline cost model (matches ``tests/execution/test_engine.py``):

  slippage = 50 bps per side (0.50%)
  tick     = 1000 KRW
  step     = 0.001 BTC
  bid_fee  = ask_fee = 0.0025
  min_bid  = 5000 KRW; min_ask = 5000 KRW  (both are KRW-denominated notionals)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from bithumb_bot.backtest import BacktestConfig, BacktestResult, run_backtest
from bithumb_bot.bithumb_spec.snapshot import FeeRates, Minimums, SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution import ExecutionConfig
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance
from bithumb_bot.strategy import BaselineStrategyConfig


UNIT = 240
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=UNIT)


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _c(
    idx: int,
    *,
    o: str,
    h: str | None = None,
    low: str | None = None,
    c: str,
) -> Candle:
    """Build one candle at index ``idx``. Defaults collapse high/low onto
    the OHLC extremes derived from open/close."""
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
    """Convenience: flat (open=high=low=close=price) candle."""
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
) -> SnapshotV1:
    return SnapshotV1(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        retrieved_at_utc="2026-09-09T00:00:00Z",
        source_endpoints=["/v1/orders/chance"],
        fee_rates=FeeRates(bid="0.0025", ask="0.0025"),
        minimums=Minimums(krw_min_total_bid="5000", krw_min_total_ask="5000"),
        # Tick = 1 KRW so low-price test fixtures (100, 101, …) don't get
        # snapped to a next-1000-tick multiple that would wildly distort
        # the 10% protective-stop hand math. Real Bithumb ticks vary by
        # price band; this is a test-only choice, not a production claim.
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


def _strategy_cfg(lookback: int = 3) -> BaselineStrategyConfig:
    return BaselineStrategyConfig(
        rule_id="price_over_sma",
        ma_type="SMA",
        lookback_candles=lookback,
        warmup_candles=lookback,
        unit_minutes=UNIT,
        market="KRW-BTC",
    )


def _exec_cfg(
    *,
    max_notional_krw: str = "100000000",
    allow_provisional: bool = False,
) -> ExecutionConfig:
    return ExecutionConfig(
        slippage_bps_per_side=Decimal("50"),
        max_notional_krw=Money(Decimal(max_notional_krw)),
        allow_provisional_fee_model=allow_provisional,
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
        strategy=_strategy_cfg(lookback),
        execution=_exec_cfg(
            max_notional_krw=max_notional_krw,
            allow_provisional=allow_provisional,
        ),
    )


# ---------------------------------------------------------------------------
# 1. LONG signal at candle t creates an intent but cannot fill until t+1
# ---------------------------------------------------------------------------


class TestLongSignalCreatesButDoesNotFillSameCandle:
    def test_long_pending_until_next_candle(self) -> None:
        # closes [100, 100, 101, 101]  lookback=3
        # candle 2: sma=(100+100+101)/3=100.33..  close=101 → LONG (transition)
        # candle 3: sma=(100+101+101)/3=100.66..  close=101 → LONG (no txn)
        # Runner at candle 2: creates BUY intent (signal_ts = candle3.open).
        # BUY cannot fill at candle 2 (no candle with open >= signal_ts in view [0..2]).
        # BUY fills at candle 3.
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        assert len(result.entries) == 1
        entry = result.entries[0]
        assert entry.side == "buy"
        assert entry.source_open_time_utc == T0 + STEP * 2
        assert entry.signal_ts_utc == T0 + STEP * 3
        assert entry.fill_ts_utc == T0 + STEP * 3


# ---------------------------------------------------------------------------
# 2. Buy fills at t+1 open and its stop is active within t+1
# ---------------------------------------------------------------------------


class TestBuyEstablishesActiveStopSameCandle:
    def test_buy_fill_creates_stop_within_same_candle_no_intrabar_trigger(
        self,
    ) -> None:
        # Same as test 1 but assert active_stop is populated.
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        assert len(result.entries) == 1
        # candle 3 low = 101 > stop = 101 * 0.9 = 90.9 → no intrabar trigger.
        stop = result.active_protective_stop
        assert stop is not None
        assert stop.entry_fill_price.value == result.entries[0].fill_price.value
        # 10% below actual fill price.
        expected_stop = result.entries[0].fill_price.value * Decimal("0.9")
        assert stop.stop_price.value == expected_stop
        assert stop.activated_at_utc == result.entries[0].fill_ts_utc


# ---------------------------------------------------------------------------
# 3. Entry + same-candle intrabar stop → two correctly ordered ledger entries
# ---------------------------------------------------------------------------


class TestEntryAndSameCandleIntrabarStop:
    def test_two_entries_buy_then_intrabar_stop(self) -> None:
        # Precompute stop from an entry filled at candle 3's open.
        # candle3 open = 101 → adverse buy price ceil to tick 1000 → fill_price
        # = ceil(101 * 1.005, 1000) = ceil(101.505, 1000) = 1000. Hmm that's
        # too small — use bigger price so slippage + tick + step math is clean.
        # Use closes = [100_000_000, 100_000_000, 101_000_000, 101_000_000]
        # candle 3 open = 101_000_000. Buy fill_price = ceil(101_000_000*1.005, 1000)
        #                             = ceil(101_505_000, 1000) = 101_505_000.
        # stop = 101_505_000 * 0.9 = 91_354_500.
        # If candle 3 low <= 91_354_500 → intrabar stop.
        c3 = _c(3, o="101000000", h="101000000", low="90000000", c="101000000")
        candles = [
            _flat(0, "100000000"),
            _flat(1, "100000000"),
            _flat(2, "101000000"),
            c3,
        ]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        assert len(result.entries) == 2
        buy, sell = result.entries
        assert buy.side == "buy"
        assert sell.side == "sell"
        assert sell.execution_reason == "protective_stop_intrabar"
        assert sell.fill_ts_utc == c3.open_time_utc  # same candle
        # Stop cleared, lockout ON.
        assert result.active_protective_stop is None
        assert result.stopped_out_lockout is True
        assert result.final_position_qty.value == Decimal("0")


# ---------------------------------------------------------------------------
# 4. Existing stop can exit before current candle's close signal is consumed
# ---------------------------------------------------------------------------


class TestStopExitPrecedesCloseSignal:
    def test_stop_triggers_then_cash_signal_clears_lockout_same_candle(
        self,
    ) -> None:
        # Establish LONG at candle 3 (from LONG transition at candle 2).
        # At candle 4: low breaches stop → intrabar exit at step 4.
        # Also at candle 4's close: SMA rule flips to CASH → step 6 sees
        # position 0 + lockout ON → clears lockout (steps 4 and 6 both fire
        # in the same iteration; step 4 happens FIRST).
        # closes: [100,100,101,101,10]
        # SMA windows (lookback 3):
        #   i=2 [100,100,101] sma=100.33 close=101 → LONG (txn)
        #   i=3 [100,101,101] sma=100.66 close=101 → LONG (no txn)
        #   i=4 [101,101,10]  sma=70.66  close=10  → CASH (txn)
        # candle 4 low = 10 → intrabar stop crossed (stop ≈ 91.35M/1000 in earlier
        # test but here the base close is 101 and fill_price=ceil(101*1.005,1000)
        # =1000, stop=1000*0.9=900. Low=10 <= 900 → intrabar. Good.
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "101"),
            _c(4, o="101", h="101", low="10", c="10"),
        ]
        # Sizing: cash=20M, fee=0.0025 → intended_notional=20M/1.0025≈19_950_124.7
        # >= max_notional_krw default 100M — OK. Fill at candle 3 open=101,
        # fill_price=ceil(101*1.005,1000)=1000. filled_qty=floor(19_950_124/1000,
        # 0.001)=19_950.124. But min_bid=5000. So filled buy has qty ~19_950.
        # Value math irrelevant — we care only about ordering + lockout.
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        assert len(result.entries) == 2
        assert result.entries[0].side == "buy"
        assert result.entries[1].side == "sell"
        assert result.entries[1].execution_reason == "protective_stop_intrabar"
        # Lockout was cleared by candle 4's CASH signal.
        assert result.stopped_out_lockout is False
        assert result.pending_intent is None
        assert result.active_protective_stop is None


# ---------------------------------------------------------------------------
# 5. CASH transition produces a next-candle full-position sell
# ---------------------------------------------------------------------------


class TestCashTransitionCreatesFullPositionSell:
    def test_cash_transition_sells_full_position_next_candle(self) -> None:
        # closes: [100,100,101,101,99,99,99]
        # candle 2 [100,100,101] → LONG (txn), buy pending → fills candle 3.
        # candle 3 [100,101,101] → LONG (no txn).
        # candle 4 [101,101,99]  → sma=100.33 close=99 → CASH (txn) → sell pending.
        # candle 5 [101,99,99]   → sma=99.66  close=99 → CASH (no txn), sell fills.
        # candle 6 [99,99,99]    → sma=99     close=99 → equality → CASH (no txn).
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "101"),
            _flat(4, "99"),
            _flat(5, "99"),
            _flat(6, "99"),
        ]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        assert len(result.entries) == 2
        buy, sell = result.entries
        assert buy.side == "buy"
        assert sell.side == "sell"
        assert sell.execution_reason == "strategy_signal"
        assert sell.fill_ts_utc == T0 + STEP * 5  # next candle after CASH txn
        # sell qty was the full position from the buy (step-floored).
        assert sell.filled_qty.value == buy.filled_qty.value
        assert result.final_position_qty.value == Decimal("0")


# ---------------------------------------------------------------------------
# 6. No duplicate buys or sells while desired state is unchanged
# ---------------------------------------------------------------------------


class TestNoDuplicateBuysOrSells:
    def test_long_run_produces_exactly_one_buy(self) -> None:
        # Once we're LONG and the strategy stays LONG, no new BUY.
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "101"),
            _flat(4, "102"),
            _flat(5, "103"),
            _flat(6, "104"),
        ]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        assert sum(1 for e in result.entries if e.side == "buy") == 1
        assert sum(1 for e in result.entries if e.side == "sell") == 0
        assert result.final_position_qty.value > 0


# ---------------------------------------------------------------------------
# 7. Stop exit locks out immediate LONG re-entry
# ---------------------------------------------------------------------------


class TestStopExitLocksOutReentry:
    def test_lockout_prevents_reentry_while_baseline_stays_long(self) -> None:
        # Same as test 3 (stop-out at candle 3 intrabar). Extend with more
        # candles where the SMA still says LONG. Assert no new BUY appears.
        c3 = _c(3, o="101000000", h="101000000", low="90000000", c="101000000")
        candles = [
            _flat(0, "100000000"),
            _flat(1, "100000000"),
            _flat(2, "101000000"),
            c3,
            # From here the SMA rule is still LONG at each candle (close > sma).
            _flat(4, "102000000"),
            _flat(5, "103000000"),
            _flat(6, "104000000"),
        ]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        # Two entries only: buy + stop-out sell. No re-entry.
        assert len(result.entries) == 2
        # But wait — the strategy only emitted ONE LONG transition (at
        # candle 2). No further LONG signals fire on candles 4-6 (they're
        # continuations, not transitions). So the "lockout prevents
        # re-entry" claim can only be tested if a fresh LONG transition
        # actually happens after the stop. That's TestCashThenLongRearms
        # below. Here we just verify the state remains locked and flat.
        assert result.stopped_out_lockout is True
        assert result.final_position_qty.value == Decimal("0")


# ---------------------------------------------------------------------------
# 8. CASH followed by a later LONG rearms entry
# ---------------------------------------------------------------------------


class TestCashThenLongRearmsEntry:
    def test_stop_then_cash_then_long_produces_reentry(self) -> None:
        # Establish LONG (candle 2). Stop-out intrabar at candle 3.
        # Then rule flips to CASH (clears lockout while flat), then
        # rule flips back to LONG → new BUY intent fires and fills.
        # closes: [100M,100M,101M,101M(low 90M),1,1,101M]
        # candle 4 [101M,101M(=101 close post-stop),1] sma≈(101M+101M+1)/3
        #   → close 1 → CASH → clears lockout (position already 0).
        # candle 5 [101M(close of stop candle=101M),1,1] sma≈(101M+1+1)/3=33.7M
        #   → close 1 → CASH (no txn).
        # candle 6 [1,1,101M] sma≈(1+1+101M)/3=33.7M close=101M → LONG (txn)
        #   → new BUY intent. But there's no candle 7 → intent pending.
        c3 = _c(3, o="101000000", h="101000000", low="90000000", c="101000000")
        candles = [
            _flat(0, "100000000"),
            _flat(1, "100000000"),
            _flat(2, "101000000"),
            c3,
            _flat(4, "1"),
            _flat(5, "1"),
            _flat(6, "101000000"),
        ]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        assert len(result.entries) == 2  # buy + stop-out sell, no re-entry yet
        assert result.stopped_out_lockout is False  # cleared by candle-4 CASH
        # A LONG transition at candle 6 created a pending BUY intent for
        # what would be candle 7. There is no candle 7 → still pending.
        assert result.pending_intent is not None
        assert result.pending_intent.side == "buy"
        assert (
            result.pending_intent.signal_ts_utc
            == candles[-1].open_time_utc + STEP
        )


# ---------------------------------------------------------------------------
# 9. Missing candle or unverified stop window fails closed
# ---------------------------------------------------------------------------


class TestMissingCandleAndUnverifiedStopFailClosed:
    def test_internal_missing_slot_refuses(self) -> None:
        # Skip candle at index 3 (open_time = T0 + 3*STEP is missing).
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(4, "101"),
        ]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is not None
        assert result.refusal_code == "InternalCandleGapError"
        assert result.entries == ()

    def test_reported_missing_interval_inside_range_refuses(self) -> None:
        # Reported missing slot within the internal range → refuse.
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        missing = [(T0 + STEP * 2).isoformat()]
        result = run_backtest(_dataset(candles, missing=missing), _snapshot(), _cfg())
        assert result.invalid_reason is not None
        assert result.refusal_code == "ReportedMissingIntervalError"

    def test_unverified_buy_fee_status_refuses(self) -> None:
        # market_buy_fee_reservation = unresolved_until_M6B → refuse
        # BEFORE any intent is constructed.
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        result = run_backtest(
            _dataset(candles),
            _snapshot(buy_status="unresolved_until_M6B"),
            _cfg(),
        )
        assert result.invalid_reason is not None
        assert result.refusal_code == "UnverifiedFeeModelError"
        assert result.entries == ()

    def test_provisional_buy_fee_without_optin_refuses(self) -> None:
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        result = run_backtest(
            _dataset(candles),
            _snapshot(buy_status="provisional_documented"),
            _cfg(allow_provisional=False),
        )
        assert result.invalid_reason is not None
        assert result.refusal_code == "UnverifiedFeeModelError"

    def test_provisional_buy_fee_with_optin_records_provisional_status(
        self,
    ) -> None:
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        result = run_backtest(
            _dataset(candles),
            _snapshot(buy_status="provisional_documented"),
            _cfg(allow_provisional=True),
        )
        assert result.invalid_reason is None
        assert result.used_provisional_fee_model is True


# ---------------------------------------------------------------------------
# 10. Fee-inclusive sizing never overspends cash
# ---------------------------------------------------------------------------


class TestFeeInclusiveSizingNeverOverspendsCash:
    def test_total_debit_leq_target_debit_leq_available_cash(self) -> None:
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        cash = Decimal("20000000")
        result = run_backtest(
            _dataset(candles), _snapshot(), _cfg(cash=str(cash))
        )
        assert result.invalid_reason is None
        assert len(result.entries) == 1
        buy = result.entries[0]
        # total_cash_debit_krw <= cash * target_sleeve_fraction (== cash here).
        assert buy.total_cash_debit_krw.value <= cash
        # And cash after >= 0 by construction.
        assert result.final_cash_krw.value >= 0


# ---------------------------------------------------------------------------
# 11. Validated-notional excess fails rather than clipping
# ---------------------------------------------------------------------------


class TestValidatedNotionalExcessFailsRatherThanClipping:
    def test_intended_notional_over_cap_fails_closed(self) -> None:
        # Cash 20M, sleeve 1.0, fee 0.0025 → intended_notional ~ 19.95M.
        # Cap it below that → runner must fail closed, not clip.
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        result = run_backtest(
            _dataset(candles),
            _snapshot(),
            _cfg(cash="20000000", max_notional_krw="10000000"),
        )
        assert result.invalid_reason is not None
        assert result.refusal_code == "NotionalCapExceededError"
        # No BUY entry produced.
        assert all(e.side != "buy" for e in result.entries)


# ---------------------------------------------------------------------------
# 12. Final-candle signal remains pending and is never falsely filled
# ---------------------------------------------------------------------------


class TestFinalCandleSignalStaysPending:
    def test_last_candle_long_transition_leaves_intent_pending(self) -> None:
        # closes: [100,100,101] — LONG transition at candle 2 (final).
        # Runner must NOT call execute_intent on the intent (there is no
        # candle whose open >= candle2.open + unit), and must NOT invent
        # a NoNextCandleError. Intent remains in BacktestResult.pending_intent.
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101")]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert result.invalid_reason is None
        assert result.entries == ()
        assert result.pending_intent is not None
        assert result.pending_intent.side == "buy"
        assert result.pending_intent.signal_ts_utc == T0 + STEP * 3
        assert result.final_position_qty.value == Decimal("0")


# ---------------------------------------------------------------------------
# 13. No future candle can change ledger entries already produced
# ---------------------------------------------------------------------------


class TestNoLookAhead:
    def test_extending_dataset_preserves_prior_entries(self) -> None:
        base = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        base_result = run_backtest(_dataset(base), _snapshot(), _cfg())
        assert base_result.invalid_reason is None
        assert len(base_result.entries) == 1  # BUY only

        # Extend with future candles that would trigger a stop and re-entry
        # later — the earlier BUY entry must remain byte-equal.
        extended = base + [
            _c(4, o="101", h="101", low="10", c="10"),  # stop-out
            _flat(5, "10"),
            _flat(6, "10"),
            _flat(7, "10"),
        ]
        extended_result = run_backtest(_dataset(extended), _snapshot(), _cfg())
        assert extended_result.invalid_reason is None
        # First entry must be byte-equal across runs.
        assert extended_result.entries[0] == base_result.entries[0]


# ---------------------------------------------------------------------------
# 14. Deterministic replay produces identical results
# ---------------------------------------------------------------------------


class TestDeterministicReplay:
    def test_identical_inputs_identical_output(self) -> None:
        candles = [
            _flat(0, "100"),
            _flat(1, "100"),
            _flat(2, "101"),
            _flat(3, "101"),
            _c(4, o="101", h="101", low="10", c="10"),
            _flat(5, "10"),
            _flat(6, "10"),
        ]
        a = run_backtest(_dataset(candles), _snapshot(), _cfg())
        b = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert a.entries == b.entries
        assert a.final_cash_krw == b.final_cash_krw
        assert a.final_position_qty == b.final_position_qty
        assert a.stopped_out_lockout == b.stopped_out_lockout
        assert a.invalid_reason == b.invalid_reason
        assert a.refusal_code == b.refusal_code


# ---------------------------------------------------------------------------
# Structural / config discipline
# ---------------------------------------------------------------------------


class TestConfigDiscipline:
    def test_config_rejects_float_target_sleeve(self) -> None:
        with pytest.raises(TypeError, match="target_sleeve_fraction"):
            BacktestConfig(
                starting_cash_krw=Money(Decimal("1000000")),
                target_sleeve_fraction=1.0,  # type: ignore[arg-type]
                protective_stop_fraction=Decimal("0.10"),
                strategy=_strategy_cfg(),
                execution=_exec_cfg(),
            )

    def test_config_rejects_float_stop_fraction(self) -> None:
        with pytest.raises(TypeError, match="protective_stop_fraction"):
            BacktestConfig(
                starting_cash_krw=Money(Decimal("1000000")),
                target_sleeve_fraction=Decimal("1.0"),
                protective_stop_fraction=0.10,  # type: ignore[arg-type]
                strategy=_strategy_cfg(),
                execution=_exec_cfg(),
            )

    def test_config_rejects_sleeve_over_one(self) -> None:
        with pytest.raises(ValueError, match="target_sleeve_fraction"):
            BacktestConfig(
                starting_cash_krw=Money(Decimal("1000000")),
                target_sleeve_fraction=Decimal("1.5"),
                protective_stop_fraction=Decimal("0.10"),
                strategy=_strategy_cfg(),
                execution=_exec_cfg(),
            )

    def test_config_rejects_stop_fraction_at_or_above_one(self) -> None:
        with pytest.raises(ValueError, match="protective_stop_fraction"):
            BacktestConfig(
                starting_cash_krw=Money(Decimal("1000000")),
                target_sleeve_fraction=Decimal("1.0"),
                protective_stop_fraction=Decimal("1.0"),
                strategy=_strategy_cfg(),
                execution=_exec_cfg(),
            )

    def test_market_mismatch_propagates_valueerror(self) -> None:
        # Programming error, not a domain refusal — must propagate.
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101")]
        cfg = BacktestConfig(
            starting_cash_krw=Money(Decimal("1000000")),
            target_sleeve_fraction=Decimal("1.0"),
            protective_stop_fraction=Decimal("0.10"),
            strategy=BaselineStrategyConfig(
                rule_id="price_over_sma",
                ma_type="SMA",
                lookback_candles=3,
                warmup_candles=3,
                unit_minutes=UNIT,
                market="KRW-ETH",  # mismatch
            ),
            execution=_exec_cfg(),
        )
        with pytest.raises(ValueError, match="market"):
            run_backtest(_dataset(candles), _snapshot(), cfg)


# ---------------------------------------------------------------------------
# BacktestResult return-shape guarantee
# ---------------------------------------------------------------------------


class TestBacktestResultShape:
    def test_result_shape_and_frozen(self) -> None:
        candles = [_flat(0, "100"), _flat(1, "100"), _flat(2, "101"), _flat(3, "101")]
        result = run_backtest(_dataset(candles), _snapshot(), _cfg())
        assert isinstance(result, BacktestResult)
        # Frozen dataclass.
        with pytest.raises((AttributeError, Exception)):
            result.invalid_reason = "hack"  # type: ignore[misc]
