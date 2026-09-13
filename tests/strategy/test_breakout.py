"""Focused tests for the breakout shadow-candidate signal generator.

Covers:

* Entry boundary — strict-greater-than ``prior_120_high * (1 + 50 bps)``.
* Exit boundary — strict-less-than ``max(entry_breakout_level, prior_60_low)``.
* Equality retains state at both boundaries.
* Transitions emitted only on state change.
* Next-candle execution invariant — ``signal_ts_utc ==
  source_open_time_utc + unit_minutes``.
* Deterministic replay — same inputs → equal outputs.
* Future-append prefix invariance.
* Structural input validation — market / unit / contiguity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bithumb_bot.market_data.candles import Candle
from bithumb_bot.strategy.breakout import (
    ENTRY_LOOKBACK_CANDLES,
    EXIT_LOOKBACK_CANDLES,
    BreakoutSignal,
    generate_breakout_signals,
)

UNIT = 240
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=UNIT)


def _candle(
    idx: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    market: str = "KRW-BTC",
    unit: int = UNIT,
) -> Candle:
    return Candle(
        market=market,
        unit_minutes=unit,
        open_time_utc=T0 + idx * timedelta(minutes=unit),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume="1",
        quote_volume=close,
    )


def _flat(idx: int, price: str) -> Candle:
    return _candle(idx, open_=price, high=price, low=price, close=price)


def _warmup_flat(price: str, count: int = ENTRY_LOOKBACK_CANDLES) -> list[Candle]:
    return [_flat(i, price) for i in range(count)]


class TestEntryBoundary:
    def test_close_equal_to_entry_boundary_retains_cash(self) -> None:
        # prior_120_high = 100_000; upper = 100_000 * 1.005 = 100_500.
        # A close of exactly 100_500 is equality → retain CASH.
        warmup = _warmup_flat("100000")
        boundary = _candle(
            ENTRY_LOOKBACK_CANDLES,
            open_="100000",
            high="100500",
            low="100000",
            close="100500",
        )
        signals = generate_breakout_signals(warmup + [boundary])
        assert signals == ()

    def test_close_one_unit_above_boundary_enters_long(self) -> None:
        warmup = _warmup_flat("100000")
        breach = _candle(
            ENTRY_LOOKBACK_CANDLES,
            open_="100000",
            high="100501",
            low="100000",
            close="100501",
        )
        signals = generate_breakout_signals(warmup + [breach])
        assert [s.target_state for s in signals] == ["LONG"]
        # entry_breakout_level is the RAW prior_120_high (unbuffered).
        # prior_120_high == 100_000 here — the 50-bps buffer participates
        # only in the entry test, not in the persisted level.
        assert signals[0].entry_breakout_level == Decimal("100000")
        assert signals[0].prior_high == Decimal("100000")

    def test_close_just_below_boundary_retains_cash(self) -> None:
        warmup = _warmup_flat("100000")
        below = _candle(
            ENTRY_LOOKBACK_CANDLES,
            open_="100000",
            high="100499",
            low="100000",
            close="100499",
        )
        signals = generate_breakout_signals(warmup + [below])
        assert signals == ()


class TestExitBoundary:
    def _enter_long_series(self, breach_close: str = "200000") -> list[Candle]:
        # Warmup + 1 breakout candle that pushes far above upper.
        return _warmup_flat("100000") + [
            _candle(
                ENTRY_LOOKBACK_CANDLES,
                open_="100000",
                high=breach_close,
                low="100000",
                close=breach_close,
            )
        ]

    def test_close_equal_to_max_of_entry_and_60low_retains_long(self) -> None:
        # After a big breakout (close=200_000), entry_breakout_level =
        # RAW prior_120_high = 100_000. Prior-60-low from the exit
        # candle's window = 100_000 (all warmup candles at 100_000 and
        # the breakout candle's low is 100_000). Exit threshold =
        # max(100_000, 100_000) = 100_000. A close of exactly 100_000
        # on the exit candle retains LONG (equality).
        series = self._enter_long_series()
        exit_candle = _candle(
            ENTRY_LOOKBACK_CANDLES + 1,
            open_="200000",
            high="200000",
            low="100000",
            close="100000",
        )
        signals = generate_breakout_signals(series + [exit_candle])
        assert [s.target_state for s in signals] == ["LONG"]

    def test_close_one_unit_below_exit_threshold_exits(self) -> None:
        series = self._enter_long_series()
        # Exit threshold = max(entry_breakout_level=100_000,
        # prior_60_low=100_000) = 100_000. close < 100_000 exits.
        exit_candle = _candle(
            ENTRY_LOOKBACK_CANDLES + 1,
            open_="200000",
            high="200000",
            low="99999",
            close="99999",
        )
        signals = generate_breakout_signals(series + [exit_candle])
        assert [s.target_state for s in signals] == ["LONG", "CASH"]
        # Exit signal carries the same RAW entry_breakout_level.
        assert signals[1].entry_breakout_level == Decimal("100000")

    def test_prior_60_low_ratchets_above_entry_level(self) -> None:
        # After entry, if the prior_60_low rises above the entry level,
        # the exit threshold becomes that higher low. A close between
        # the entry level and the prior_60_low fires an exit.
        # Warm-up entirely at 100_000; entry at 200_000; then 60 candles
        # all above 100_500 to lift prior_60_low above 100_500; then the
        # exit candle at 100_500 exactly should EXIT (because
        # prior_60_low is now > 100_500).
        warmup = _warmup_flat("100000")
        entry = _candle(
            ENTRY_LOOKBACK_CANDLES,
            open_="100000",
            high="200000",
            low="150000",
            close="200000",
        )
        # 60 flat candles at 200_000 -> prior_60_low from the next
        # candle's window = 200_000 (all lows above entry level).
        holding = [
            _flat(ENTRY_LOOKBACK_CANDLES + 1 + k, "200000")
            for k in range(EXIT_LOOKBACK_CANDLES)
        ]
        # Exit candle with close well below prior_60_low = 200_000.
        exit_candle = _candle(
            ENTRY_LOOKBACK_CANDLES + 1 + EXIT_LOOKBACK_CANDLES,
            open_="200000",
            high="200000",
            low="199999",
            close="199999",
        )
        signals = generate_breakout_signals(
            warmup + [entry] + holding + [exit_candle]
        )
        assert [s.target_state for s in signals] == ["LONG", "CASH"]


class TestTransitionsOnlyOnStateChange:
    def test_no_signal_when_state_unchanged_across_run(self) -> None:
        # Warmup flat; every subsequent candle stays inside the band →
        # zero transitions.
        candles = _warmup_flat("100000") + [
            _flat(ENTRY_LOOKBACK_CANDLES + k, "100000") for k in range(5)
        ]
        signals = generate_breakout_signals(candles)
        assert signals == ()

    def test_single_entry_only_on_first_breach(self) -> None:
        # Multiple sustained-above-upper candles produce ONE entry signal.
        warmup = _warmup_flat("100000")
        breaches = [
            _flat(ENTRY_LOOKBACK_CANDLES + k, "300000") for k in range(5)
        ]
        signals = generate_breakout_signals(warmup + breaches)
        assert len(signals) == 1
        assert signals[0].target_state == "LONG"


class TestNextCandleExecutionInvariant:
    def test_signal_ts_equals_source_close_boundary(self) -> None:
        warmup = _warmup_flat("100000")
        breach = _flat(ENTRY_LOOKBACK_CANDLES, "300000")
        drop = _candle(
            ENTRY_LOOKBACK_CANDLES + 1,
            open_="300000",
            high="300000",
            low="50000",
            close="50000",
        )
        signals = generate_breakout_signals(warmup + [breach, drop])
        assert len(signals) == 2
        for s in signals:
            assert isinstance(s, BreakoutSignal)
            assert (
                s.signal_ts_utc
                == s.source_open_time_utc
                + timedelta(minutes=s.unit_minutes)
            )


class TestDeterministicReplay:
    def test_same_inputs_same_output(self) -> None:
        warmup = _warmup_flat("100000")
        breach = _flat(ENTRY_LOOKBACK_CANDLES, "300000")
        drop = _flat(ENTRY_LOOKBACK_CANDLES + 1, "50000")
        candles = warmup + [breach, drop]
        a = generate_breakout_signals(candles)
        b = generate_breakout_signals(candles)
        assert a == b
        for x, y in zip(a, b, strict=True):
            assert x is not y
            assert x == y


class TestFutureAppendInvariance:
    def test_prefix_is_stable_when_future_candles_are_appended(self) -> None:
        warmup = _warmup_flat("100000")
        breach = _flat(ENTRY_LOOKBACK_CANDLES, "300000")
        base = warmup + [breach]
        base_signals = generate_breakout_signals(base)
        assert [s.target_state for s in base_signals] == ["LONG"]

        extended = base + [
            _flat(len(base) + k, price)
            for k, price in enumerate(["300000", "50000", "50000", "500000"])
        ]
        extended_signals = generate_breakout_signals(extended)
        assert extended_signals[: len(base_signals)] == base_signals


class TestStructuralValidation:
    def test_rejects_wrong_market(self) -> None:
        candles = _warmup_flat("100000")
        bad = _candle(
            ENTRY_LOOKBACK_CANDLES,
            open_="100000",
            high="200000",
            low="100000",
            close="200000",
            market="KRW-ETH",
        )
        with pytest.raises(ValueError, match="market"):
            generate_breakout_signals(candles + [bad])

    def test_rejects_wrong_unit_minutes(self) -> None:
        candles = _warmup_flat("100000")
        bad = _candle(
            ENTRY_LOOKBACK_CANDLES,
            open_="100000",
            high="200000",
            low="100000",
            close="200000",
            unit=60,
        )
        with pytest.raises(ValueError, match="unit_minutes"):
            generate_breakout_signals(candles + [bad])

    def test_rejects_non_contiguous(self) -> None:
        candles = _warmup_flat("100000")
        # Skip one slot.
        bad = _candle(
            ENTRY_LOOKBACK_CANDLES + 2,
            open_="100000",
            high="200000",
            low="100000",
            close="200000",
        )
        with pytest.raises(ValueError, match="non-contiguous"):
            generate_breakout_signals(candles + [bad])

    def test_warmup_returns_empty_below_entry_lookback(self) -> None:
        signals = generate_breakout_signals(
            _warmup_flat("100000", count=ENTRY_LOOKBACK_CANDLES)
        )
        # Exactly ENTRY_LOOKBACK candles: i=0..119. First evaluable index
        # is 120, so no candle triggers the entry check.
        assert signals == ()

    def test_empty_input_returns_empty(self) -> None:
        assert generate_breakout_signals([]) == ()
