"""Focused hand-calculated tests for the ``price_over_sma`` baseline.

Test fixtures use ``lookback_candles = 3`` (with matching
``warmup_candles = 3``) so the SMA is trivially hand-verifiable
(``sum(last 3 closes) / 3``). Production behavior at the frozen
lookback of 1,200 is covered by the ``test_production_config_*``
tests separately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bithumb_bot.market_data.candles import Candle
from bithumb_bot.strategy import (
    BaselineStrategyConfig,
    StrategySignal,
    generate_signals,
)


UNIT = 240
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=UNIT)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _candle(
    idx: int,
    close_str: str,
    *,
    market: str = "KRW-BTC",
    unit: int = UNIT,
) -> Candle:
    """Minimal Candle with a chosen close. OHLC collapsed onto close."""
    return Candle(
        market=market,
        unit_minutes=unit,
        open_time_utc=T0 + timedelta(minutes=unit * idx),
        open=close_str,
        high=close_str,
        low=close_str,
        close=close_str,
        volume="1",
        quote_volume=close_str,
    )


def _series(closes: list[str]) -> list[Candle]:
    return [_candle(i, c) for i, c in enumerate(closes)]


def _cfg(
    lookback: int = 3, *, hysteresis_bps: str = "0"
) -> BaselineStrategyConfig:
    """Hand-verified test config.

    Default ``hysteresis_bps="0"`` collapses the band so existing
    scenarios keep their pre-hysteresis semantics (the only behavior
    difference at h=0 is that equality with the SMA now retains the
    current state — no existing scenario relies on the older
    equality-goes-CASH-from-LONG snap).
    """
    return BaselineStrategyConfig(
        rule_id="price_over_sma",
        ma_type="SMA",
        lookback_candles=lookback,
        warmup_candles=lookback,
        unit_minutes=UNIT,
        market="KRW-BTC",
        hysteresis_bps=Decimal(hysteresis_bps),
    )


# ---------------------------------------------------------------------------
# warm-up
# ---------------------------------------------------------------------------


def test_warmup_returns_empty_below_lookback() -> None:
    # 2 candles, lookback 3 → warm-up not complete → no signals.
    signals = generate_signals(_series(["100", "110"]), _cfg(3))
    assert signals == ()


def test_warmup_returns_empty_at_exactly_lookback_minus_one() -> None:
    # lookback=3, feed exactly 2 candles → still warm-up.
    signals = generate_signals(_series(["100", "100"]), _cfg(3))
    assert signals == ()


def test_warmup_completes_at_exactly_lookback() -> None:
    # 3 closes = 100,110,120 → SMA = 110, close = 120 > 110 → LONG.
    signals = generate_signals(_series(["100", "110", "120"]), _cfg(3))
    assert len(signals) == 1
    assert signals[0].target_state == "LONG"
    assert signals[0].sma_value == Decimal("110")
    assert signals[0].close_value == Decimal("120")


def test_warmup_does_not_raise_on_insufficient_history() -> None:
    # Confirm normal warm-up is NOT an error (no InsufficientHistoryError).
    # Empty input, single candle, and lookback-1 candles all return () quietly.
    cfg = _cfg(3)
    assert generate_signals([], cfg) == ()
    assert generate_signals(_series(["100"]), cfg) == ()
    assert generate_signals(_series(["100", "110"]), cfg) == ()


# ---------------------------------------------------------------------------
# first evaluable candle — initial CASH state semantics
# ---------------------------------------------------------------------------


def test_first_evaluable_candle_cash_state_emits_nothing() -> None:
    # 3 closes = 100,110,105 → SMA = 105, close = 105 → equality → CASH.
    # Initial state is CASH, so no transition → no signal.
    signals = generate_signals(_series(["100", "110", "105"]), _cfg(3))
    assert signals == ()


def test_first_evaluable_candle_long_state_emits_long() -> None:
    # 3 closes = 100,100,101 → SMA ≈ 100.33..., close = 101 > SMA → LONG.
    signals = generate_signals(_series(["100", "100", "101"]), _cfg(3))
    assert len(signals) == 1
    assert signals[0].target_state == "LONG"


def test_equality_maps_to_cash() -> None:
    # close == SMA must be CASH (strict > only).
    signals = generate_signals(_series(["100", "100", "100"]), _cfg(3))
    assert signals == ()


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------


def test_entry_transition_cash_to_long() -> None:
    # Windows (3-candle SMA), rule state per candle (from i=2 onward):
    #   i=2  closes [100,100,99]   sma=99.66..  close=99  → CASH  (no txn)
    #   i=3  closes [100,99,101]   sma=100      close=101 → LONG  (txn 1)
    signals = generate_signals(_series(["100", "100", "99", "101"]), _cfg(3))
    assert [s.target_state for s in signals] == ["LONG"]
    # Signal timestamp is the close boundary of candle i=3.
    assert signals[0].signal_ts_utc == T0 + STEP * 4
    assert signals[0].source_open_time_utc == T0 + STEP * 3


def test_exit_transition_long_to_cash() -> None:
    # Windows:
    #   i=2 closes [100,100,101]  sma=100.33..  close=101 → LONG  (txn 1)
    #   i=3 closes [100,101,99]   sma=100       close=99  → CASH  (txn 2)
    signals = generate_signals(_series(["100", "100", "101", "99"]), _cfg(3))
    assert [s.target_state for s in signals] == ["LONG", "CASH"]
    assert signals[0].source_open_time_utc == T0 + STEP * 2
    assert signals[1].source_open_time_utc == T0 + STEP * 3


def test_no_duplicate_transition_when_state_unchanged() -> None:
    # Every evaluable candle rules LONG — expect exactly ONE signal
    # (at the first evaluable candle), no duplicates thereafter.
    signals = generate_signals(
        _series(["100", "100", "101", "102", "103", "104", "105"]),
        _cfg(3),
    )
    assert len(signals) == 1
    assert signals[0].target_state == "LONG"
    assert signals[0].source_open_time_utc == T0 + STEP * 2


def test_no_duplicate_transition_across_cash_run() -> None:
    # Windows post-warmup all yield CASH; initial state is CASH → 0 signals.
    signals = generate_signals(
        _series(["100", "100", "99", "98", "97", "96"]),
        _cfg(3),
    )
    assert signals == ()


# ---------------------------------------------------------------------------
# exact signal timestamp
# ---------------------------------------------------------------------------


def test_signal_timestamp_is_source_close_boundary() -> None:
    # Ensure signal_ts_utc == source_open_time_utc + unit_minutes for every
    # emitted signal (matches OrderIntent.buy_from_signal convention).
    signals = generate_signals(
        _series(["100", "100", "101", "99", "102"]),
        _cfg(3),
    )
    for signal in signals:
        assert (
            signal.signal_ts_utc
            == signal.source_open_time_utc + timedelta(minutes=signal.unit_minutes)
        )


# ---------------------------------------------------------------------------
# no-look-ahead
# ---------------------------------------------------------------------------


def test_no_look_ahead_extending_history_preserves_past_signals() -> None:
    # Full 6-candle series produces some prefix of signals; running the
    # same generator on any leading prefix must produce a byte-equal
    # prefix of that output.
    full = _series(["100", "100", "101", "99", "102", "98"])
    cfg = _cfg(3)
    full_signals = generate_signals(full, cfg)
    # Sanity: this series does produce transitions.
    assert len(full_signals) > 0
    for k in range(3, len(full) + 1):
        partial_signals = generate_signals(full[:k], cfg)
        # partial signals must be an exact prefix of full signals.
        assert partial_signals == full_signals[: len(partial_signals)]


def test_no_look_ahead_appending_future_never_mutates_past() -> None:
    # Concretely: compute signals at candle i, then append many future
    # candles. Every previously-emitted signal is byte-equal in the new
    # output for the same index range.
    base = _series(["100", "100", "101", "99"])
    cfg = _cfg(3)
    base_signals = generate_signals(base, cfg)
    extended = base + [
        _candle(i, c)
        for i, c in enumerate(["104", "108", "112", "110", "115"], start=len(base))
    ]
    extended_signals = generate_signals(extended, cfg)
    assert extended_signals[: len(base_signals)] == base_signals


# ---------------------------------------------------------------------------
# deterministic replay
# ---------------------------------------------------------------------------


def test_deterministic_replay_same_inputs_same_output() -> None:
    candles = _series(["100", "100", "101", "99", "102", "103", "97", "98"])
    cfg = _cfg(3)
    a = generate_signals(candles, cfg)
    b = generate_signals(candles, cfg)
    assert a == b
    # StrategySignal is a frozen dataclass → equality is field-wise.
    for x, y in zip(a, b, strict=True):
        assert isinstance(x, StrategySignal)
        assert x is not y  # separate instances
        assert x == y      # but field-equal


# ---------------------------------------------------------------------------
# structural input validation (distinct from warm-up)
# ---------------------------------------------------------------------------


def test_wrong_market_raises() -> None:
    bad = [_candle(0, "100", market="KRW-ETH")] + _series(["100", "101"])[1:]
    with pytest.raises(ValueError, match="market"):
        generate_signals(bad, _cfg(3))


def test_wrong_unit_minutes_raises() -> None:
    bad = [_candle(0, "100", unit=60)] + _series(["100", "101"])[1:]
    with pytest.raises(ValueError, match="unit_minutes"):
        generate_signals(bad, _cfg(3))


def test_non_contiguous_sequence_raises() -> None:
    # Skip candle at index 1 → gap between idx 0 and idx 2.
    a = _candle(0, "100")
    c = _candle(2, "102")
    with pytest.raises(ValueError, match="non-contiguous"):
        generate_signals([a, c], _cfg(3))


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_production_config_frozen_values() -> None:
    cfg = BaselineStrategyConfig.production()
    assert cfg.rule_id == "price_over_sma"
    assert cfg.ma_type == "SMA"
    assert cfg.lookback_candles == 1_200
    assert cfg.warmup_candles == 1_200
    assert cfg.unit_minutes == 240
    assert cfg.market == "KRW-BTC"
    assert cfg.hysteresis_bps == Decimal("75")


def test_production_config_is_frozen_instance() -> None:
    cfg = BaselineStrategyConfig.production()
    with pytest.raises(ValidationError):
        cfg.lookback_candles = 900  # type: ignore[misc]


def test_config_requires_all_fields_no_defaults() -> None:
    with pytest.raises(ValidationError):
        BaselineStrategyConfig()  # type: ignore[call-arg]


def test_config_rejects_wrong_rule_id() -> None:
    with pytest.raises(ValidationError):
        BaselineStrategyConfig(
            rule_id="ema_cross",  # type: ignore[arg-type]
            ma_type="SMA",
            lookback_candles=3,
            warmup_candles=3,
            unit_minutes=UNIT,
            market="KRW-BTC",
            hysteresis_bps=Decimal("0"),
        )


def test_config_rejects_wrong_ma_type() -> None:
    with pytest.raises(ValidationError):
        BaselineStrategyConfig(
            rule_id="price_over_sma",
            ma_type="EMA",  # type: ignore[arg-type]
            lookback_candles=3,
            warmup_candles=3,
            unit_minutes=UNIT,
            market="KRW-BTC",
            hysteresis_bps=Decimal("0"),
        )


def test_config_rejects_warmup_lookback_mismatch() -> None:
    with pytest.raises(ValidationError, match="warmup_candles"):
        BaselineStrategyConfig(
            rule_id="price_over_sma",
            ma_type="SMA",
            lookback_candles=3,
            warmup_candles=4,
            unit_minutes=UNIT,
            market="KRW-BTC",
            hysteresis_bps=Decimal("0"),
        )


def test_config_rejects_zero_or_negative_lookback() -> None:
    with pytest.raises(ValidationError):
        BaselineStrategyConfig(
            rule_id="price_over_sma",
            ma_type="SMA",
            lookback_candles=0,
            warmup_candles=0,
            unit_minutes=UNIT,
            market="KRW-BTC",
            hysteresis_bps=Decimal("0"),
        )


def test_config_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        BaselineStrategyConfig(
            rule_id="price_over_sma",
            ma_type="SMA",
            lookback_candles=3,
            warmup_candles=3,
            unit_minutes=UNIT,
            market="KRW-BTC",
            hysteresis_bps=Decimal("0"),
            optimize=True,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# hysteresis: config validation
# ---------------------------------------------------------------------------


def _cfg_with_hyst(value: object) -> BaselineStrategyConfig:
    """Direct construction to probe hysteresis validation only."""
    return BaselineStrategyConfig(
        rule_id="price_over_sma",
        ma_type="SMA",
        lookback_candles=3,
        warmup_candles=3,
        unit_minutes=UNIT,
        market="KRW-BTC",
        hysteresis_bps=value,  # type: ignore[arg-type]
    )


def test_hysteresis_bps_rejects_float() -> None:
    with pytest.raises(ValidationError):
        _cfg_with_hyst(75.0)


def test_hysteresis_bps_rejects_int() -> None:
    # Bare int construction is a D-49 float-adjacent shortcut; reject.
    with pytest.raises(ValidationError):
        _cfg_with_hyst(75)


def test_hysteresis_bps_rejects_str() -> None:
    with pytest.raises(ValidationError):
        _cfg_with_hyst("75")


def test_hysteresis_bps_rejects_bool() -> None:
    with pytest.raises(ValidationError):
        _cfg_with_hyst(True)


def test_hysteresis_bps_rejects_negative() -> None:
    with pytest.raises(ValidationError, match="hysteresis_bps"):
        _cfg_with_hyst(Decimal("-1"))


def test_hysteresis_bps_rejects_at_or_above_10000() -> None:
    with pytest.raises(ValidationError, match="hysteresis_bps"):
        _cfg_with_hyst(Decimal("10000"))
    with pytest.raises(ValidationError, match="hysteresis_bps"):
        _cfg_with_hyst(Decimal("50000"))


def test_hysteresis_bps_accepts_boundary_zero_and_just_below_10000() -> None:
    cfg_zero = _cfg_with_hyst(Decimal("0"))
    cfg_near = _cfg_with_hyst(Decimal("9999.9999"))
    assert cfg_zero.hysteresis_bps == Decimal("0")
    assert cfg_near.hysteresis_bps == Decimal("9999.9999")


# ---------------------------------------------------------------------------
# hysteresis: rule behavior (hand-calculated at h = 75 bps)
# ---------------------------------------------------------------------------

# Hand math for lookback=3 at h=75 bps:
#   sma = sum / 3; upper = sma * 1.0075; lower = sma * 0.9925.
# Choose SMA=100 so numbers are trivial → upper=100.75, lower=99.25.


class TestHysteresisEntry:
    def test_no_long_entry_inside_upper_band(self) -> None:
        # Windows: [100,100,100] → sma=100, close=100 (inside band).
        # Then [100,100,100.5] → sma=100.166..., close=100.5 (< 100.166*1.0075 ≈ 100.917) — inside band.
        # Neither candle transitions from CASH.
        signals = generate_signals(
            _series(["100", "100", "100", "100.5"]),
            _cfg(3, hysteresis_bps="75"),
        )
        assert signals == ()

    def test_long_entry_strictly_above_upper_boundary(self) -> None:
        # Warm-up + [100,100,101] → sma=100.333..., upper=100.333*1.0075 ≈ 101.086.
        # close=101 is BELOW the upper boundary → no entry.
        # Then [100,101,102] → sma=101, upper=101*1.0075=101.7575.
        # close=102 > 101.7575 → LONG entry fires.
        signals = generate_signals(
            _series(["100", "100", "101", "102"]),
            _cfg(3, hysteresis_bps="75"),
        )
        assert [s.target_state for s in signals] == ["LONG"]
        assert signals[0].source_open_time_utc == T0 + STEP * 3

    def test_equality_at_upper_boundary_retains_cash(self) -> None:
        # sma=100, upper=100.75, close=100.75 exactly → retain CASH.
        # closes: [100.75, 100.75, 99.5, 100.75]
        # i=2 window [100.75, 100.75, 99.5]  sum=301.0  sma=100.333.. upper=101.086.. close=99.5 (below) → CASH
        # i=3 window [100.75, 99.5, 100.75]  sum=301    sma=100.333.. upper=101.086.. close=100.75 (below) → CASH
        # Neither ever equals its upper boundary. Simplify to construct exact equality:
        # window [100, 100, 100.75]  sum=300.75  sma=100.25  upper=100.25*1.0075=100.9990625..
        # No integer close hits it exactly. Build the exact case algebraically:
        #   choose sum such that sum * 10075 / 30000 == close exactly.
        # Simpler: sum=30000, lookback=3 → sma=10000 (Decimal exact).
        # upper = 10000 * 1.0075 = 10075. Pick closes [10000, 10000, 10075] — but
        # then sum = 30075, not 30000. Instead: [10075, 10000, 10075] sum=30150,
        # sma=10050, upper=10125.375 — doesn't equal any close.
        # Direct symbolic construction (satisfies scaled_close == running_sum * upper_factor):
        #   scaled_close = close * 3 * 10000
        #   sum * (10000 + 75) = sum * 10075
        #   choose sum = 30000 (so sma=10000) → RHS = 302_250_000
        #   → close * 30000 = 302_250_000 → close = 10_075.
        # So window [x, y, z] with x+y+z=30000 AND z=10075.
        #   → x + y = 19_925. Pick x=10000, y=9925.
        # Warm-up needs 2 candles before the evaluable one, so:
        candles = _series(["10000", "9925", "10075"])
        signals = generate_signals(candles, _cfg(3, hysteresis_bps="75"))
        assert signals == ()  # equality retains CASH


class TestHysteresisRetentionInsideBand:
    def test_long_position_retained_while_inside_band(self) -> None:
        # Force LONG at i=3 via close well above upper. Then i=4..5 keep close
        # inside the band → retain LONG (no CASH transition).
        # Sequence chosen so entry fires cleanly:
        candles = _series(["100", "100", "100", "200", "150", "150"])
        # i=2 [100,100,100] sma=100 upper=100.75 close=100 (inside) → CASH
        # i=3 [100,100,200] sma=133.33 upper=134.33 close=200 >> upper → LONG entry
        # i=4 [100,200,150] sma=150 lower=148.875 close=150 (inside, > lower) → retain LONG
        # i=5 [200,150,150] sma=166.67 lower=165.42 close=150 < lower → CASH exit
        # So signals should be [LONG @ i=3, CASH @ i=5].
        # Restrict to only i=3 and i=4 to verify pure retention:
        signals = generate_signals(
            candles[:5],  # up through i=4
            _cfg(3, hysteresis_bps="75"),
        )
        assert [s.target_state for s in signals] == ["LONG"]
        assert signals[0].source_open_time_utc == T0 + STEP * 3


class TestHysteresisExit:
    def test_no_cash_exit_inside_lower_band(self) -> None:
        # Get into LONG, then place close just above lower boundary.
        candles = _series(["100", "100", "100", "200", "149"])
        # i=3: sma=133.33 upper=134.33 close=200 → LONG entry.
        # i=4: window [100,200,149] sum=449 sma=149.667 lower=149.667*0.9925=148.545..
        #      close=149 > 148.545 → retain LONG (no exit).
        signals = generate_signals(candles, _cfg(3, hysteresis_bps="75"))
        assert [s.target_state for s in signals] == ["LONG"]

    def test_cash_exit_strictly_below_lower_boundary(self) -> None:
        # Same entry, but close just below the lower boundary.
        # window [100,200,148] sum=448 sma=149.333 lower=149.333*0.9925=148.198..
        # close=148 < 148.198 → CASH exit fires.
        candles = _series(["100", "100", "100", "200", "148"])
        signals = generate_signals(candles, _cfg(3, hysteresis_bps="75"))
        assert [s.target_state for s in signals] == ["LONG", "CASH"]
        assert signals[1].source_open_time_utc == T0 + STEP * 4

    def test_equality_at_lower_boundary_retains_long(self) -> None:
        # Build symbolic equality at the FINAL candle while keeping the
        # intermediate windows LONG:
        #   Target final window sum=30000 with close=9925 →
        #     scaled_close = 9925 * 3 * 10000    = 297_750_000
        #     lower_rhs    = 30000 * (10000-75) = 297_750_000  (equal)
        #   Intermediate windows must satisfy scaled_close >= lower_rhs.
        # Series [1000, 10075, 10075, 10075, 10000, 9925]:
        #   i=2 window sum=21150, close=10075 → LONG entry (well above upper).
        #   i=3 window sum=30225, close=10075 → retain LONG (302.25M > 299.98M).
        #   i=4 window sum=30150, close=10000 → retain LONG (300M > 299.24M).
        #   i=5 window sum=30000, close=9925  → equality → retain LONG.
        candles = _series([
            "1000", "10075", "10075", "10075", "10000", "9925",
        ])
        signals = generate_signals(candles, _cfg(3, hysteresis_bps="75"))
        # Only the LONG entry fires; equality at the lower boundary does
        # NOT emit a CASH transition.
        assert [s.target_state for s in signals] == ["LONG"]


class TestHysteresisNoDuplicateTransitions:
    def test_no_duplicate_long_or_cash(self) -> None:
        # Entry once, exit once — no double LONG, no double CASH.
        candles = _series([
            "100", "100", "100",
            "200",  # LONG entry (clearly above upper)
            "200",  # retain LONG
            "50",   # CASH exit (clearly below lower)
            "50",   # retain CASH
        ])
        signals = generate_signals(candles, _cfg(3, hysteresis_bps="75"))
        assert [s.target_state for s in signals] == ["LONG", "CASH"]


class TestHysteresisNoLookAhead:
    def test_appending_future_candles_preserves_prior_signals(self) -> None:
        base = _series(["100", "100", "100", "200", "200"])
        cfg = _cfg(3, hysteresis_bps="75")
        base_signals = generate_signals(base, cfg)
        extended = base + [
            _candle(i, c)
            for i, c in enumerate(["50", "50", "400"], start=len(base))
        ]
        extended_signals = generate_signals(extended, cfg)
        assert extended_signals[: len(base_signals)] == base_signals


class TestHysteresisDeterministicReplay:
    def test_same_inputs_same_output(self) -> None:
        candles = _series([
            "100", "100", "100", "200", "150", "50", "50", "400",
        ])
        cfg = _cfg(3, hysteresis_bps="75")
        a = generate_signals(candles, cfg)
        b = generate_signals(candles, cfg)
        assert a == b


class TestHysteresisSignalTimestampInvariant:
    def test_signal_ts_matches_source_close_boundary(self) -> None:
        candles = _series(["100", "100", "100", "200", "50"])
        signals = generate_signals(candles, _cfg(3, hysteresis_bps="75"))
        assert len(signals) >= 1
        for s in signals:
            assert (
                s.signal_ts_utc
                == s.source_open_time_utc
                + timedelta(minutes=s.unit_minutes)
            )
