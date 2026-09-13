"""Focused functional tests for ``order_flow.breakout``.

Every branch of ``dispatch_breakout_signal`` is exercised exactly once
against a real ``MockBroker`` on ``tmp_path`` — no network, no HTTP,
no credentials, no float math.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from bithumb_bot.broker.mock import MockBroker
from bithumb_bot.broker.state import OrderState
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent
from bithumb_bot.order_flow.breakout import (
    DispatchRefused,
    dispatch_breakout_signal,
)
from bithumb_bot.strategy.breakout import BreakoutSignal, TargetState

_UNIT = 240
_SOURCE_OPEN = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_SIGNAL_TS = _SOURCE_OPEN + timedelta(minutes=_UNIT)


def _long_signal() -> BreakoutSignal:
    return BreakoutSignal(
        target_state="LONG",
        signal_ts_utc=_SIGNAL_TS,
        source_open_time_utc=_SOURCE_OPEN,
        unit_minutes=_UNIT,
        close_value=Decimal("100500000"),
        prior_high=Decimal("100000000"),
        prior_low=None,
        entry_breakout_level=Decimal("100000000"),
    )


def _cash_signal() -> BreakoutSignal:
    return BreakoutSignal(
        target_state="CASH",
        signal_ts_utc=_SIGNAL_TS,
        source_open_time_utc=_SOURCE_OPEN,
        unit_minutes=_UNIT,
        close_value=Decimal("95000000"),
        prior_high=None,
        prior_low=Decimal("99000000"),
        entry_breakout_level=Decimal("100000000"),
    )


def _order_files(root: Path) -> list[Path]:
    return sorted((root / "orders").glob("*.json"))


def _fee() -> Decimal:
    return Decimal("0.0025")


def test_long_creates_fee_aware_buy_intent(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = dispatch_breakout_signal(
        signal=_long_signal(),
        cash=Money.from_str("100000"),
        position=Qty.from_str("0"),
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("100000"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )
    assert order is not None
    assert order.state == OrderState.ACCEPTED
    assert order.intent.side == "buy"
    assert order.intent.requested_qty is None
    assert order.intent.requested_notional_krw is not None
    expected_notional = Decimal("100000") / (Decimal("1") + _fee())
    assert order.intent.requested_notional_krw.value == expected_notional
    assert order.intent.source_open_time_utc == _SOURCE_OPEN
    assert order.intent.signal_ts_utc == _SIGNAL_TS
    assert len(_order_files(tmp_path)) == 1


def test_cash_creates_full_position_sell_intent(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    position = Qty.from_str("0.5")
    order = dispatch_breakout_signal(
        signal=_cash_signal(),
        cash=Money.from_str("0"),
        position=position,
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("999999999"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )
    assert order is not None
    assert order.intent.side == "sell"
    assert order.intent.requested_notional_krw is None
    assert order.intent.requested_qty == position
    assert len(_order_files(tmp_path)) == 1


def test_boundary_observed_at_equal_signal_ts_accepted(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = dispatch_breakout_signal(
        signal=_long_signal(),
        cash=Money.from_str("50000"),
        position=Qty.from_str("0"),
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("50000"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )
    assert order is not None
    assert len(_order_files(tmp_path)) == 1


def test_earlier_observed_at_refused_without_files(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(
            signal=_long_signal(),
            cash=Money.from_str("50000"),
            position=Qty.from_str("0"),
            bid_fee_rate=_fee(),
            max_notional_krw=Money.from_str("50000"),
            stopped_out_lockout=False,
            observed_at_utc=_SIGNAL_TS - timedelta(microseconds=1),
            broker=broker,
        )
    assert _order_files(tmp_path) == []


def test_duplicate_submission_creates_one_order(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = dict(
        signal=_long_signal(),
        cash=Money.from_str("100000"),
        position=Qty.from_str("0"),
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("100000"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )
    first = dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    second = dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert first is not None and second is not None
    assert first == second
    assert len(_order_files(tmp_path)) == 1


def test_restart_then_retry_creates_no_duplicate(tmp_path: Path) -> None:
    kwargs = dict(
        signal=_long_signal(),
        cash=Money.from_str("100000"),
        position=Qty.from_str("0"),
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("100000"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
    )
    b1 = MockBroker(store_root=tmp_path)
    first = dispatch_breakout_signal(broker=b1, **kwargs)  # type: ignore[arg-type]
    del b1
    b2 = MockBroker(store_root=tmp_path)
    second = dispatch_breakout_signal(broker=b2, **kwargs)  # type: ignore[arg-type]
    assert first is not None and second is not None
    assert first.client_order_id == second.client_order_id
    assert first == second
    assert len(_order_files(tmp_path)) == 1


def test_different_unresolved_order_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    # Pre-existing accepted order that is NOT the one we're about to
    # dispatch — a stale sell sitting open, for example.
    other_intent = OrderIntent(
        side="sell",
        source_open_time_utc=_SOURCE_OPEN - timedelta(minutes=_UNIT),
        unit_minutes=_UNIT,
        signal_ts_utc=_SOURCE_OPEN,
        requested_notional_krw=None,
        requested_qty=Qty.from_str("0.25"),
    )
    broker.submit(other_intent)
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(
            signal=_long_signal(),
            cash=Money.from_str("100000"),
            position=Qty.from_str("0"),
            bid_fee_rate=_fee(),
            max_notional_krw=Money.from_str("100000"),
            stopped_out_lockout=False,
            observed_at_utc=_SIGNAL_TS,
            broker=broker,
        )
    # Only the pre-existing order file remains — dispatch wrote nothing.
    assert len(_order_files(tmp_path)) == 1


def test_long_during_lockout_creates_no_order(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = dispatch_breakout_signal(
        signal=_long_signal(),
        cash=Money.from_str("100000"),
        position=Qty.from_str("0"),
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("100000"),
        stopped_out_lockout=True,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )
    assert order is None
    assert _order_files(tmp_path) == []


def test_long_with_existing_position_creates_no_order(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = dispatch_breakout_signal(
        signal=_long_signal(),
        cash=Money.from_str("100000"),
        position=Qty.from_str("0.1"),
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("100000"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )
    assert order is None
    assert _order_files(tmp_path) == []


def test_cash_with_zero_position_creates_no_order(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = dispatch_breakout_signal(
        signal=_cash_signal(),
        cash=Money.from_str("0"),
        position=Qty.from_str("0"),
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("100000"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )
    assert order is None
    assert _order_files(tmp_path) == []


def test_notional_cap_refuses_before_broker_mutation(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    # cash / (1 + fee) = 200000 / 1.0025 ≈ 199501.24 > 100000 cap
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(
            signal=_long_signal(),
            cash=Money.from_str("200000"),
            position=Qty.from_str("0"),
            bid_fee_rate=_fee(),
            max_notional_krw=Money.from_str("100000"),
            stopped_out_lockout=False,
            observed_at_utc=_SIGNAL_TS,
            broker=broker,
        )
    assert _order_files(tmp_path) == []


def _valid_long_kwargs(broker: MockBroker) -> dict[str, object]:
    return dict(
        signal=_long_signal(),
        cash=Money.from_str("100000"),
        position=Qty.from_str("0"),
        bid_fee_rate=_fee(),
        max_notional_krw=Money.from_str("100000"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )


def test_unknown_target_state_with_position_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    # An unknown target must NEVER be treated as CASH — a bug in a
    # future strategy that emitted "STAY" must not liquidate the book.
    bogus = replace(_long_signal(), target_state=cast(TargetState, "STAY"))
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(
            signal=bogus,
            cash=Money.from_str("0"),
            position=Qty.from_str("0.25"),
            bid_fee_rate=_fee(),
            max_notional_krw=Money.from_str("100000"),
            stopped_out_lockout=False,
            observed_at_utc=_SIGNAL_TS,
            broker=broker,
        )
    assert _order_files(tmp_path) == []


@pytest.mark.parametrize(
    "bad_fee",
    [
        Decimal("-0.5"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1"),
        cast(Decimal, 0.0025),
        cast(Decimal, True),
        cast(Decimal, False),
    ],
)
def test_invalid_bid_fee_rate_refused(tmp_path: Path, bad_fee: object) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = _valid_long_kwargs(broker)
    kwargs["bid_fee_rate"] = bad_fee
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert _order_files(tmp_path) == []


@pytest.mark.parametrize(
    "field",
    ["observed_at_utc", "signal.source_open_time_utc", "signal.signal_ts_utc"],
)
def test_naive_timestamps_refused(tmp_path: Path, field: str) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = _valid_long_kwargs(broker)
    naive_source = _SOURCE_OPEN.replace(tzinfo=None)
    naive_signal = _SIGNAL_TS.replace(tzinfo=None)
    if field == "observed_at_utc":
        kwargs["observed_at_utc"] = naive_signal
    elif field == "signal.source_open_time_utc":
        kwargs["signal"] = replace(_long_signal(), source_open_time_utc=naive_source)
    else:
        kwargs["signal"] = replace(_long_signal(), signal_ts_utc=naive_signal)
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert _order_files(tmp_path) == []


def test_wrong_unit_minutes_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = _valid_long_kwargs(broker)
    # signal_ts_utc stays consistent (source + 60), only the unit is wrong.
    kwargs["signal"] = replace(
        _long_signal(),
        unit_minutes=60,
        signal_ts_utc=_SOURCE_OPEN + timedelta(minutes=60),
    )
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert _order_files(tmp_path) == []


def test_inconsistent_signal_ts_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = _valid_long_kwargs(broker)
    kwargs["signal"] = replace(
        _long_signal(),
        signal_ts_utc=_SIGNAL_TS + timedelta(minutes=1),
    )
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert _order_files(tmp_path) == []


@pytest.mark.parametrize(
    "cash_value",
    [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_invalid_cash_refused(tmp_path: Path, cash_value: Decimal) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = _valid_long_kwargs(broker)
    kwargs["cash"] = Money(cash_value)
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert _order_files(tmp_path) == []


@pytest.mark.parametrize(
    "pos_value",
    [Decimal("-0.1"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_invalid_position_refused(tmp_path: Path, pos_value: Decimal) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = _valid_long_kwargs(broker)
    kwargs["position"] = Qty(pos_value)
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert _order_files(tmp_path) == []


@pytest.mark.parametrize(
    "cap_value",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_invalid_max_notional_refused(tmp_path: Path, cap_value: Decimal) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = _valid_long_kwargs(broker)
    kwargs["max_notional_krw"] = Money(cap_value)
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert _order_files(tmp_path) == []


@pytest.mark.parametrize("bad_lockout", [0, 1, "false", None])
def test_non_bool_lockout_refused(tmp_path: Path, bad_lockout: object) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = _valid_long_kwargs(broker)
    kwargs["stopped_out_lockout"] = bad_lockout
    with pytest.raises(DispatchRefused):
        dispatch_breakout_signal(**kwargs)  # type: ignore[arg-type]
    assert _order_files(tmp_path) == []


def test_decimal_arithmetic_remains_exact(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    cash = Money.from_str("100000.00000001")
    fee = Decimal("0.00250001")
    order = dispatch_breakout_signal(
        signal=_long_signal(),
        cash=cash,
        position=Qty.from_str("0"),
        bid_fee_rate=fee,
        max_notional_krw=Money.from_str("100000.00000001"),
        stopped_out_lockout=False,
        observed_at_utc=_SIGNAL_TS,
        broker=broker,
    )
    assert order is not None
    assert order.intent.requested_notional_krw is not None
    expected = cash.value / (Decimal("1") + fee)
    got = order.intent.requested_notional_krw.value
    assert got == expected
    assert isinstance(got, Decimal)
