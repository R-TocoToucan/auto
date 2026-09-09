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


def _cfg(lookback: int = 3) -> BaselineStrategyConfig:
    return BaselineStrategyConfig(
        rule_id="price_over_sma",
        ma_type="SMA",
        lookback_candles=lookback,
        warmup_candles=lookback,
        unit_minutes=UNIT,
        market="KRW-BTC",
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
            optimize=True,  # type: ignore[call-arg]
        )
