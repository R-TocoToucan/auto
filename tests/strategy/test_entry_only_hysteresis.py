"""Focused functional tests for the entry-only hysteresis rule.

Rule under test (see ``bithumb_bot/strategy/baseline.py``):

    CASH -> LONG:  close * lookback * 10000 > running_sum * (10000 + h)
    LONG -> CASH:  close * lookback * 10000 < running_sum * 10000

Equality retains the current state. Hysteresis is the entry buffer;
exit is at the SMA itself.

Covers:

* Entry boundary — strict-greater-than the upper (SMA * (1 + h)) band.
* Exit below and at the SMA — strict-less-than fires, equality retains.
* State retention in both directions.
* Deterministic replay — same inputs → equal outputs.
* Future-append invariance — appending future candles never mutates
  previously emitted signals.
* Next-candle execution — every emitted signal's ``signal_ts_utc``
  equals ``source_open_time_utc + unit_minutes`` (the close boundary
  that becomes the earliest fill candle for the execution engine).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bithumb_bot.market_data.candles import Candle
from bithumb_bot.strategy import (
    BaselineStrategyConfig,
    generate_signals,
)

UNIT = 240
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=UNIT)


def _candle(idx: int, close: str) -> Candle:
    return Candle(
        market="KRW-BTC",
        unit_minutes=UNIT,
        open_time_utc=T0 + idx * STEP,
        open=close,
        high=close,
        low=close,
        close=close,
        volume="1",
        quote_volume=close,
    )


def _series(closes: list[str]) -> list[Candle]:
    return [_candle(i, c) for i, c in enumerate(closes)]


def _cfg(hysteresis_bps: str = "75") -> BaselineStrategyConfig:
    return BaselineStrategyConfig(
        rule_id="price_over_sma",
        ma_type="SMA",
        lookback_candles=3,
        warmup_candles=3,
        unit_minutes=UNIT,
        market="KRW-BTC",
        hysteresis_bps=Decimal(hysteresis_bps),
    )


class TestEntryBoundary:
    def test_close_at_upper_boundary_retains_cash(self) -> None:
        # sum=30000, close=10075 → sma=10025, upper=sum*10075/30000=10075.125
        # No — construct symbolic equality:
        #   scaled_close = close * 3 * 10000 = close * 30000
        #   upper_rhs    = sum * 10075
        #   equality:  close * 30000 == sum * 10075
        #   choose sum=30000 → close*30000 == 30000*10075 → close=10075.
        #   window sum=30000 with close=10075 → x0+x1 = 30000-10075 = 19925.
        # Series [10000, 9925, 10075]:
        candles = _series(["10000", "9925", "10075"])
        signals = generate_signals(candles, _cfg("75"))
        # Equality at the upper boundary — CASH retained, no entry.
        assert signals == ()

    def test_close_strictly_above_upper_boundary_enters_long(self) -> None:
        # Same shape, close nudged one unit above equality:
        candles = _series(["10000", "9925", "10076"])
        # sum=30001, close*30000=302_280_000; sum*10075=302_260_075.
        # scaled_close > upper_rhs → LONG entry fires.
        signals = generate_signals(candles, _cfg("75"))
        assert [s.target_state for s in signals] == ["LONG"]

    def test_close_just_below_upper_boundary_retains_cash(self) -> None:
        candles = _series(["10000", "9925", "10074"])
        # sum=29999, close*30000=302_220_000; sum*10075=302_239_925.
        # scaled_close < upper_rhs → retain CASH.
        signals = generate_signals(candles, _cfg("75"))
        assert signals == ()


class TestExitAtSMA:
    def test_close_equal_to_sma_retains_long(self) -> None:
        # Enter LONG at i=2, land exactly on SMA at i=3.
        #   Series [100, 100, 200, 150]:
        #     i=2 sum=400, close=200 → upper_rhs=400*10075=4_030_000;
        #         scaled=200*30000=6_000_000 → LONG entry.
        #     i=3 sum=450, close=150 → sma=150; exit_rhs=450*10000=4_500_000;
        #         scaled=150*30000=4_500_000 → equality → retain LONG.
        candles = _series(["100", "100", "200", "150"])
        signals = generate_signals(candles, _cfg("75"))
        assert [s.target_state for s in signals] == ["LONG"]

    def test_close_strictly_below_sma_exits_long(self) -> None:
        # Same LONG entry, but close one unit below the SMA at i=3.
        candles = _series(["100", "100", "200", "149"])
        # i=3 sum=449, sma=149.66..; scaled=149*30000=4_470_000;
        # exit_rhs=449*10000=4_490_000 → scaled < exit_rhs → CASH exit.
        signals = generate_signals(candles, _cfg("75"))
        assert [s.target_state for s in signals] == ["LONG", "CASH"]

    def test_close_inside_old_symmetric_lower_band_now_exits(self) -> None:
        # Under the OLD symmetric rule (exit at SMA*(1-h/10000)),
        # close=149 with sma=149.667 stayed LONG because 149 > lower≈148.545.
        # Under the entry-only rule, exit is at the SMA — close < 149.667
        # so CASH exit fires. This test locks in the intentional behavior
        # change vs. the previous baseline.
        candles = _series(["100", "100", "200", "149"])
        signals = generate_signals(candles, _cfg("75"))
        assert signals[-1].target_state == "CASH"


class TestStateRetentionBothDirections:
    def test_cash_state_retained_across_flat_series(self) -> None:
        # Flat closes → sma equals close every candle → equality → retain
        # CASH throughout.
        candles = _series(["100", "100", "100", "100", "100"])
        signals = generate_signals(candles, _cfg("75"))
        assert signals == ()

    def test_long_state_retained_across_flat_series_above_sma(self) -> None:
        # Enter LONG cleanly, then flat closes at or above the SMA →
        # retain LONG for the rest of the series.
        candles = _series(["100", "100", "300", "300", "300"])
        # i=2 sum=500, close=300 → upper_rhs=500*10075=5_037_500;
        #     scaled=300*30000=9_000_000 → LONG entry.
        # i=3 sum=700, close=300 → sma=233.33; close>sma → retain LONG.
        # i=4 sum=900, close=300 → sma=300; equality → retain LONG.
        signals = generate_signals(candles, _cfg("75"))
        assert [s.target_state for s in signals] == ["LONG"]


class TestDeterministicReplay:
    def test_identical_inputs_yield_equal_outputs(self) -> None:
        candles = _series(["100", "100", "200", "150", "149", "160"])
        cfg = _cfg("75")
        a = generate_signals(candles, cfg)
        b = generate_signals(candles, cfg)
        assert a == b
        # Different instances, field-equal.
        for x, y in zip(a, b, strict=True):
            assert x is not y
            assert x == y


class TestFutureAppendInvariance:
    def test_appending_future_candles_never_mutates_prior_signals(
        self,
    ) -> None:
        base = _series(["100", "100", "200", "150"])
        cfg = _cfg("75")
        base_signals = generate_signals(base, cfg)
        # base emits exactly the LONG entry at i=2 (i=3 is equality →
        # retain LONG).
        assert [s.target_state for s in base_signals] == ["LONG"]

        extended = base + [
            _candle(len(base) + k, c)
            for k, c in enumerate(["149", "160", "100", "50"])
        ]
        extended_signals = generate_signals(extended, cfg)
        # Extended must contain at least the base's signals as an exact,
        # byte-equal prefix.
        assert extended_signals[: len(base_signals)] == base_signals


class TestNextCandleExecution:
    def test_every_signal_ts_equals_source_close_boundary(self) -> None:
        # signal_ts_utc == source_open_time_utc + unit_minutes is the
        # invariant the execution engine relies on to fill on the NEXT
        # candle (never same-candle).
        candles = _series(["100", "100", "200", "149", "160", "50"])
        signals = generate_signals(candles, _cfg("75"))
        assert len(signals) >= 1
        for s in signals:
            assert (
                s.signal_ts_utc
                == s.source_open_time_utc
                + timedelta(minutes=s.unit_minutes)
            )
