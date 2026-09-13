"""Focused tests for OrderIntent — protective-stop reason surface.

These cover only the M6A-precursor scaffold: the new ``reason`` /
``trigger_price`` fields, the timing invariants that discriminate the
three reasons, the dedicated ``protective_sell`` constructor, and the
client_order_id sensitivity to reason / trigger time / trigger price.
The existing strategy-signal behavior is exercised by
``tests/broker/test_mock_broker.py`` and stays unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bithumb_bot.broker.mock import deterministic_client_order_id
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent

BASE_OPEN = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
UNIT = 240
CLOSE = BASE_OPEN + timedelta(minutes=UNIT)


# ---------------------------------------------------------------------------
# 1. Existing strategy-signal intents keep working unchanged
# ---------------------------------------------------------------------------


def test_strategy_signal_buy_still_valid() -> None:
    intent = OrderIntent(
        side="buy",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
        signal_ts_utc=CLOSE,
        requested_notional_krw=Money.from_str("100000"),
        requested_qty=None,
    )
    assert intent.reason == "strategy_signal"
    assert intent.trigger_price is None


def test_strategy_signal_sell_still_valid() -> None:
    intent = OrderIntent(
        side="sell",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
        signal_ts_utc=CLOSE,
        requested_notional_krw=None,
        requested_qty=Qty.from_str("0.5"),
    )
    assert intent.reason == "strategy_signal"
    assert intent.trigger_price is None


def test_strategy_signal_with_trigger_price_rejected() -> None:
    with pytest.raises(ValueError, match="trigger_price"):
        OrderIntent(
            side="buy",
            source_open_time_utc=BASE_OPEN,
            unit_minutes=UNIT,
            signal_ts_utc=CLOSE,
            requested_notional_krw=Money.from_str("100000"),
            requested_qty=None,
            trigger_price=Money.from_str("50000000"),
        )


# ---------------------------------------------------------------------------
# 2. Valid protective sells
# ---------------------------------------------------------------------------


def test_protective_stop_gap_valid() -> None:
    intent = OrderIntent.protective_sell(
        reason="protective_stop_gap",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=BASE_OPEN,
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
    )
    assert intent.side == "sell"
    assert intent.reason == "protective_stop_gap"
    assert intent.signal_ts_utc == BASE_OPEN
    assert intent.trigger_price == Money.from_str("48000000")


def test_protective_stop_intrabar_valid() -> None:
    trigger = BASE_OPEN + timedelta(minutes=90)
    intent = OrderIntent.protective_sell(
        reason="protective_stop_intrabar",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=trigger,
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
    )
    assert intent.side == "sell"
    assert intent.reason == "protective_stop_intrabar"
    assert intent.signal_ts_utc == trigger


# ---------------------------------------------------------------------------
# 3. Invalid combinations are refused
# ---------------------------------------------------------------------------


def _protective_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        side="sell",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
        signal_ts_utc=BASE_OPEN,  # gap default
        requested_notional_krw=None,
        requested_qty=Qty.from_str("0.5"),
        reason="protective_stop_gap",
        trigger_price=Money.from_str("48000000"),
    )
    base.update(overrides)
    return base


def test_protective_stop_gap_wrong_timing_rejected() -> None:
    with pytest.raises(ValueError, match="protective_stop_gap"):
        OrderIntent(**_protective_kwargs(signal_ts_utc=BASE_OPEN + timedelta(minutes=1)))  # type: ignore[arg-type]


def test_protective_stop_intrabar_boundary_at_open_rejected() -> None:
    with pytest.raises(ValueError, match="protective_stop_intrabar"):
        OrderIntent(
            **_protective_kwargs(
                reason="protective_stop_intrabar",
                signal_ts_utc=BASE_OPEN,
            )  # type: ignore[arg-type]
        )


def test_protective_stop_intrabar_boundary_at_close_rejected() -> None:
    with pytest.raises(ValueError, match="protective_stop_intrabar"):
        OrderIntent(
            **_protective_kwargs(
                reason="protective_stop_intrabar",
                signal_ts_utc=CLOSE,
            )  # type: ignore[arg-type]
        )


def test_protective_stop_intrabar_past_close_rejected() -> None:
    with pytest.raises(ValueError, match="protective_stop_intrabar"):
        OrderIntent(
            **_protective_kwargs(
                reason="protective_stop_intrabar",
                signal_ts_utc=CLOSE + timedelta(seconds=1),
            )  # type: ignore[arg-type]
        )


def test_protective_stop_buy_side_rejected() -> None:
    with pytest.raises(ValueError, match="side='sell'"):
        OrderIntent(
            **_protective_kwargs(
                side="buy",
                requested_notional_krw=Money.from_str("100000"),
                requested_qty=None,
            )  # type: ignore[arg-type]
        )


def test_protective_stop_missing_trigger_price_rejected() -> None:
    with pytest.raises(ValueError, match="requires trigger_price"):
        OrderIntent(**_protective_kwargs(trigger_price=None))  # type: ignore[arg-type]


def test_protective_stop_zero_trigger_price_rejected() -> None:
    with pytest.raises(ValueError, match="trigger_price must be > 0"):
        OrderIntent(**_protective_kwargs(trigger_price=Money.from_str("0")))  # type: ignore[arg-type]


def test_protective_stop_negative_trigger_price_rejected() -> None:
    with pytest.raises(ValueError, match="trigger_price must be > 0"):
        OrderIntent(**_protective_kwargs(trigger_price=Money.from_str("-1")))  # type: ignore[arg-type]


def test_unknown_reason_rejected() -> None:
    with pytest.raises(ValueError, match="reason must be one of"):
        OrderIntent(
            side="buy",
            source_open_time_utc=BASE_OPEN,
            unit_minutes=UNIT,
            signal_ts_utc=CLOSE,
            requested_notional_krw=Money.from_str("100000"),
            requested_qty=None,
            reason="made-up",  # type: ignore[arg-type]
        )


def test_protective_sell_constructor_refuses_strategy_signal_reason() -> None:
    with pytest.raises(ValueError, match="protective_sell"):
        OrderIntent.protective_sell(
            reason="strategy_signal",  # type: ignore[arg-type]
            qty=Qty.from_str("0.5"),
            trigger_ts_utc=BASE_OPEN,
            trigger_price=Money.from_str("48000000"),
            source_open_time_utc=BASE_OPEN,
            unit_minutes=UNIT,
        )


def test_protective_stop_naive_timestamp_rejected() -> None:
    naive_open = datetime(2026, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        OrderIntent.protective_sell(
            reason="protective_stop_gap",
            qty=Qty.from_str("0.5"),
            trigger_ts_utc=naive_open,
            trigger_price=Money.from_str("48000000"),
            source_open_time_utc=naive_open,
            unit_minutes=UNIT,
        )


# ---------------------------------------------------------------------------
# 4. client_order_id sensitivity — reason, trigger time, trigger price
# ---------------------------------------------------------------------------


def _gap(
    trigger_ts: datetime = BASE_OPEN,
    trigger_price: str = "48000000",
    reason: str = "protective_stop_gap",
) -> OrderIntent:
    return OrderIntent(
        side="sell",
        source_open_time_utc=trigger_ts if reason == "protective_stop_gap" else BASE_OPEN,
        unit_minutes=UNIT,
        signal_ts_utc=trigger_ts,
        requested_notional_krw=None,
        requested_qty=Qty.from_str("0.5"),
        reason=reason,  # type: ignore[arg-type]
        trigger_price=Money.from_str(trigger_price),
    )


def test_cid_differs_when_reason_differs() -> None:
    open_a = BASE_OPEN
    # Same trigger instant but different reasons — one is at candle open
    # (gap), one is one minute later (intrabar).
    gap = OrderIntent.protective_sell(
        reason="protective_stop_gap",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=open_a,
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=open_a,
        unit_minutes=UNIT,
    )
    intrabar = OrderIntent.protective_sell(
        reason="protective_stop_intrabar",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=open_a + timedelta(minutes=1),
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=open_a,
        unit_minutes=UNIT,
    )
    assert deterministic_client_order_id(gap) != deterministic_client_order_id(intrabar)


def test_cid_differs_when_trigger_time_differs() -> None:
    a = OrderIntent.protective_sell(
        reason="protective_stop_intrabar",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=BASE_OPEN + timedelta(minutes=30),
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
    )
    b = OrderIntent.protective_sell(
        reason="protective_stop_intrabar",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=BASE_OPEN + timedelta(minutes=90),
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
    )
    assert deterministic_client_order_id(a) != deterministic_client_order_id(b)


def test_cid_differs_when_trigger_price_differs() -> None:
    a = OrderIntent.protective_sell(
        reason="protective_stop_gap",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=BASE_OPEN,
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
    )
    b = OrderIntent.protective_sell(
        reason="protective_stop_gap",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=BASE_OPEN,
        trigger_price=Money.from_str("48000001"),
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
    )
    assert deterministic_client_order_id(a) != deterministic_client_order_id(b)


def test_cid_stable_for_equal_protective_intents() -> None:
    a = _gap()
    b = _gap()
    assert deterministic_client_order_id(a) == deterministic_client_order_id(b)


# ---------------------------------------------------------------------------
# 5. Universal invariants — apply to every reason (incl. strategy_signal)
# ---------------------------------------------------------------------------


def _strategy_buy(
    *,
    source_open: datetime = BASE_OPEN,
    signal_ts: datetime | None = None,
    unit: object = UNIT,
    notional: Money | None = None,
) -> None:
    """Attempt to construct a strategy_signal buy intent with overrides."""
    OrderIntent(
        side="buy",
        source_open_time_utc=source_open,
        unit_minutes=unit,  # type: ignore[arg-type]
        signal_ts_utc=(
            signal_ts
            if signal_ts is not None
            else source_open + timedelta(minutes=UNIT)
        ),
        requested_notional_krw=(
            notional if notional is not None else Money.from_str("100000")
        ),
        requested_qty=None,
    )


def test_strategy_signal_naive_source_open_rejected() -> None:
    naive_open = datetime(2026, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="source_open_time_utc.*timezone-aware"):
        _strategy_buy(source_open=naive_open)


def test_strategy_signal_naive_signal_ts_rejected() -> None:
    naive_signal = (BASE_OPEN + timedelta(minutes=UNIT)).replace(tzinfo=None)
    with pytest.raises(ValueError, match="signal_ts_utc.*timezone-aware"):
        _strategy_buy(signal_ts=naive_signal)


def test_unit_minutes_zero_rejected() -> None:
    with pytest.raises(ValueError, match="unit_minutes must be > 0"):
        _strategy_buy(unit=0)


def test_unit_minutes_negative_rejected() -> None:
    with pytest.raises(ValueError, match="unit_minutes must be > 0"):
        _strategy_buy(unit=-1)


def test_unit_minutes_bool_rejected() -> None:
    with pytest.raises(ValueError, match="unit_minutes must be int"):
        _strategy_buy(unit=True)


def test_unit_minutes_non_int_rejected() -> None:
    with pytest.raises(ValueError, match="unit_minutes must be int"):
        _strategy_buy(unit="240")


def test_buy_notional_nan_rejected_without_leaking_invalid_operation() -> None:
    with pytest.raises(ValueError, match="finite"):
        _strategy_buy(notional=Money(Decimal("NaN")))


def test_buy_notional_infinity_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        _strategy_buy(notional=Money(Decimal("Infinity")))


def test_buy_notional_negative_infinity_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        _strategy_buy(notional=Money(Decimal("-Infinity")))


def test_sell_qty_nan_rejected_without_leaking_invalid_operation() -> None:
    with pytest.raises(ValueError, match="finite"):
        OrderIntent(
            side="sell",
            source_open_time_utc=BASE_OPEN,
            unit_minutes=UNIT,
            signal_ts_utc=CLOSE,
            requested_notional_krw=None,
            requested_qty=Qty(Decimal("NaN")),
        )


def test_sell_qty_infinity_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        OrderIntent(
            side="sell",
            source_open_time_utc=BASE_OPEN,
            unit_minutes=UNIT,
            signal_ts_utc=CLOSE,
            requested_notional_krw=None,
            requested_qty=Qty(Decimal("Infinity")),
        )


def test_protective_qty_nan_rejected_without_leaking_invalid_operation() -> None:
    with pytest.raises(ValueError, match="finite"):
        OrderIntent.protective_sell(
            reason="protective_stop_gap",
            qty=Qty(Decimal("NaN")),
            trigger_ts_utc=BASE_OPEN,
            trigger_price=Money.from_str("48000000"),
            source_open_time_utc=BASE_OPEN,
            unit_minutes=UNIT,
        )


def test_protective_qty_infinity_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        OrderIntent.protective_sell(
            reason="protective_stop_intrabar",
            qty=Qty(Decimal("Infinity")),
            trigger_ts_utc=BASE_OPEN + timedelta(minutes=1),
            trigger_price=Money.from_str("48000000"),
            source_open_time_utc=BASE_OPEN,
            unit_minutes=UNIT,
        )


def test_protective_trigger_price_nan_rejected() -> None:
    with pytest.raises(ValueError, match="trigger_price must be finite"):
        OrderIntent.protective_sell(
            reason="protective_stop_gap",
            qty=Qty.from_str("0.5"),
            trigger_ts_utc=BASE_OPEN,
            trigger_price=Money(Decimal("NaN")),
            source_open_time_utc=BASE_OPEN,
            unit_minutes=UNIT,
        )


def test_valid_strategy_signal_client_order_id_stable() -> None:
    """A valid strategy_signal intent still hashes; no invariant tightening
    disturbs the ID for legitimate inputs."""
    intent = OrderIntent(
        side="buy",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
        signal_ts_utc=CLOSE,
        requested_notional_krw=Money.from_str("100000"),
        requested_qty=None,
    )
    cid = deterministic_client_order_id(intent)
    assert len(cid) == 64 and all(c in "0123456789abcdef" for c in cid)


def test_strategy_signal_and_protective_at_same_instant_have_distinct_cids() -> None:
    # Contrived, but proves reason is hashed independently of timing:
    # build a strategy-signal sell at close(t) and a gap protective sell whose
    # signal instant *also* equals that same close (source_open of the next
    # candle). Same wire timestamp on signal_ts, different reasons.
    strat = OrderIntent(
        side="sell",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
        signal_ts_utc=CLOSE,
        requested_notional_krw=None,
        requested_qty=Qty.from_str("0.5"),
    )
    prot = OrderIntent.protective_sell(
        reason="protective_stop_gap",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=CLOSE,
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=CLOSE,
        unit_minutes=UNIT,
    )
    assert deterministic_client_order_id(strat) != deterministic_client_order_id(prot)
