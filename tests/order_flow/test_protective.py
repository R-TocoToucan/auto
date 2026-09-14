"""Focused tests for ``order_flow.protective``.

Every branch of ``dispatch_protective_stop`` is exercised against a real
``MockBroker`` on ``tmp_path`` — no network, no HTTP, no credentials,
no floats. Values are hand-picked so trigger classification, persisted
records, and idempotent recovery can be asserted from exact byte / hash
equality rather than from proxies.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex
from bithumb_bot.broker.interface import Broker
from bithumb_bot.broker.mock import MockBroker
from bithumb_bot.broker.state import BrokerOrder, OrderState
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent
from bithumb_bot.execution.stop import ProtectiveStop
from bithumb_bot.order_flow.protective import (
    SCHEMA_VERSION,
    ProtectiveDispatchError,
    ProtectiveDispatchRefused,
    ProtectiveDispatchResult,
    ProtectiveOrderTerminalError,
    ProtectiveTriggerRecordCorrupt,
    StopObservation,
    dispatch_protective_stop,
)

_MARKET = "KRW-BTC"
_UNIT = 240
_ACTIVATION = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_LATER_OPEN = _ACTIVATION + timedelta(minutes=_UNIT)
_STOP_PRICE = Money.from_str("48000000")
_ENTRY_PRICE = Money.from_str("50000000")
_STOP_QTY = Qty.from_str("0.5")


def _stop() -> ProtectiveStop:
    return ProtectiveStop(
        stop_price=_STOP_PRICE,
        qty=_STOP_QTY,
        activated_at_utc=_ACTIVATION,
        unit_minutes=_UNIT,
        entry_fill_price=_ENTRY_PRICE,
    )


def _later_intrabar_obs(
    *,
    observed_price: Money | None = None,
    candle_open_price: Money | None = None,
    minutes_into_candle: int = 90,
) -> StopObservation:
    return StopObservation(
        market=_MARKET,
        candle_open_time_utc=_LATER_OPEN,
        unit_minutes=_UNIT,
        observed_at_utc=_LATER_OPEN + timedelta(minutes=minutes_into_candle),
        candle_open_price=(
            candle_open_price if candle_open_price is not None
            else Money.from_str("49000000")
        ),
        observed_price=(
            observed_price if observed_price is not None
            else Money.from_str("47500000")
        ),
    )


def _later_gap_obs(
    *,
    candle_open_price: Money | None = None,
) -> StopObservation:
    price = candle_open_price if candle_open_price is not None else Money.from_str("47000000")
    return StopObservation(
        market=_MARKET,
        candle_open_time_utc=_LATER_OPEN,
        unit_minutes=_UNIT,
        observed_at_utc=_LATER_OPEN,
        candle_open_price=price,
        observed_price=price,
    )


def _activation_intrabar_obs(
    *,
    observed_price: Money | None = None,
) -> StopObservation:
    return StopObservation(
        market=_MARKET,
        candle_open_time_utc=_ACTIVATION,
        unit_minutes=_UNIT,
        observed_at_utc=_ACTIVATION + timedelta(minutes=60),
        candle_open_price=Money.from_str("49500000"),
        observed_price=(
            observed_price if observed_price is not None
            else Money.from_str("47500000")
        ),
    )


def _activation_open_obs() -> StopObservation:
    price = Money.from_str("47500000")
    return StopObservation(
        market=_MARKET,
        candle_open_time_utc=_ACTIVATION,
        unit_minutes=_UNIT,
        observed_at_utc=_ACTIVATION,
        candle_open_price=price,
        observed_price=price,
    )


def _order_files(root: Path) -> list[Path]:
    return sorted((root / "orders").glob("*.json"))


def _trigger_files(root: Path) -> list[Path]:
    d = root / "protective_triggers"
    if not d.is_dir():
        return []
    return sorted(d.iterdir())


# ---------------------------------------------------------------------------
# 1. Above-stop no-op
# ---------------------------------------------------------------------------


def test_above_stop_observation_is_noop(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=Money.from_str("49000000"))
    result = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert result == ProtectiveDispatchResult(
        triggered=False,
        order=None,
        stopped_out_lockout=False,
        recovered_from_persisted_trigger=False,
    )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


# ---------------------------------------------------------------------------
# 2. Later-candle intrabar equality triggers intrabar
# ---------------------------------------------------------------------------


def test_intrabar_equality_triggers_intrabar(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE, minutes_into_candle=30)
    result = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert result.triggered is True
    assert result.order is not None
    intent = result.order.intent
    assert intent.reason == "protective_stop_intrabar"
    assert intent.signal_ts_utc == _LATER_OPEN + timedelta(minutes=30)
    assert intent.trigger_price == _STOP_PRICE
    assert intent.source_open_time_utc == _LATER_OPEN
    assert intent.requested_qty == _STOP_QTY
    assert result.stopped_out_lockout is False
    assert result.recovered_from_persisted_trigger is False


# ---------------------------------------------------------------------------
# 3. Later-candle open-equality triggers gap at open price
# ---------------------------------------------------------------------------


def test_later_candle_open_triggers_gap(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    gap_price = Money.from_str("47000000")
    obs = _later_gap_obs(candle_open_price=gap_price)
    result = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert result.triggered is True
    assert result.order is not None
    intent = result.order.intent
    assert intent.reason == "protective_stop_gap"
    assert intent.signal_ts_utc == _LATER_OPEN
    assert intent.source_open_time_utc == _LATER_OPEN
    assert intent.trigger_price == gap_price


# ---------------------------------------------------------------------------
# 4. Activation-candle open is not classified as a gap
# ---------------------------------------------------------------------------


def test_activation_candle_open_is_not_a_gap(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    result = dispatch_protective_stop(
        observation=_activation_open_obs(),
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert result.triggered is False
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


# ---------------------------------------------------------------------------
# 5. Activation-candle later observation triggers intrabar
# ---------------------------------------------------------------------------


def test_activation_candle_intrabar_triggers(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    result = dispatch_protective_stop(
        observation=_activation_intrabar_obs(),
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert result.triggered is True
    assert result.order is not None
    intent = result.order.intent
    assert intent.reason == "protective_stop_intrabar"
    assert intent.signal_ts_utc == _ACTIVATION + timedelta(minutes=60)
    assert intent.trigger_price == _STOP_PRICE


# ---------------------------------------------------------------------------
# 6. Exact protective-intent fields (spot check for intrabar case)
# ---------------------------------------------------------------------------


def test_intent_fields_are_exact(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(
        observed_price=Money.from_str("47999999.999"),
        minutes_into_candle=137,
    )
    result = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert result.order is not None
    intent = result.order.intent
    assert intent.side == "sell"
    assert intent.reason == "protective_stop_intrabar"
    assert intent.requested_notional_krw is None
    assert intent.requested_qty == _STOP_QTY
    assert intent.trigger_price is not None
    assert intent.trigger_price.value == Decimal("48000000")
    assert intent.signal_ts_utc == _LATER_OPEN + timedelta(minutes=137)
    assert intent.source_open_time_utc == _LATER_OPEN
    assert intent.unit_minutes == _UNIT


# ---------------------------------------------------------------------------
# 7. Same trigger twice: one record + one broker order
# ---------------------------------------------------------------------------


def test_same_trigger_twice_one_record_one_order(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    kwargs = dict(
        observation=_later_intrabar_obs(observed_price=_STOP_PRICE),
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    r1 = dispatch_protective_stop(**kwargs)  # type: ignore[arg-type]
    r2 = dispatch_protective_stop(**kwargs)  # type: ignore[arg-type]
    assert r1.order is not None and r2.order is not None
    assert r1.order == r2.order
    assert r1.recovered_from_persisted_trigger is False
    assert r2.recovered_from_persisted_trigger is True
    assert len(_order_files(tmp_path)) == 1
    assert len([p for p in _trigger_files(tmp_path) if p.suffix == ".json"]) == 1


# ---------------------------------------------------------------------------
# 8. Restart returns same client_order_id
# ---------------------------------------------------------------------------


def test_restart_returns_same_client_order_id(tmp_path: Path) -> None:
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    b1 = MockBroker(store_root=tmp_path)
    r1 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=b1,
    )
    del b1
    b2 = MockBroker(store_root=tmp_path)
    r2 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=b2,
    )
    assert r1.order is not None and r2.order is not None
    assert r1.order.client_order_id == r2.order.client_order_id
    assert r2.recovered_from_persisted_trigger is True
    assert len(_order_files(tmp_path)) == 1


# ---------------------------------------------------------------------------
# 9. Different later trigger cannot replace persisted first trigger
# ---------------------------------------------------------------------------


def test_different_later_trigger_cannot_replace_first(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    first_obs = _later_intrabar_obs(
        observed_price=_STOP_PRICE, minutes_into_candle=30
    )
    r1 = dispatch_protective_stop(
        observation=first_obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert r1.order is not None
    original_cid = r1.order.client_order_id
    # Second observation with a different reason (gap) at a different
    # candle. Would classify differently if evaluated fresh.
    later_gap = _later_gap_obs(candle_open_price=Money.from_str("46000000"))
    later_gap = replace(
        later_gap,
        candle_open_time_utc=_LATER_OPEN + timedelta(minutes=_UNIT),
        observed_at_utc=_LATER_OPEN + timedelta(minutes=_UNIT),
    )
    r2 = dispatch_protective_stop(
        observation=later_gap,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert r2.order is not None
    assert r2.order.client_order_id == original_cid
    assert r2.order.intent.reason == "protective_stop_intrabar"
    assert r2.order.intent.signal_ts_utc == _LATER_OPEN + timedelta(minutes=30)
    assert r2.recovered_from_persisted_trigger is True
    assert len(_order_files(tmp_path)) == 1


# ---------------------------------------------------------------------------
# 10. Failure after persistence but before submit recovers on restart
# ---------------------------------------------------------------------------


class _RaiseOnSubmitBroker:
    """Broker adapter that raises on ``submit``. Other methods delegate."""

    def __init__(self, inner: MockBroker) -> None:
        self._inner = inner

    def submit(self, intent: OrderIntent) -> BrokerOrder:  # noqa: ARG002
        raise RuntimeError("simulated failure between persist and submit")

    def get(self, client_order_id: str) -> BrokerOrder | None:
        return self._inner.get(client_order_id)

    def list_open(self) -> list[BrokerOrder]:
        return self._inner.list_open()

    def cancel(self, client_order_id: str) -> BrokerOrder:
        return self._inner.cancel(client_order_id)


def test_failure_between_persist_and_submit_recovers(tmp_path: Path) -> None:
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    inner = MockBroker(store_root=tmp_path)
    failing = _RaiseOnSubmitBroker(inner)
    with pytest.raises(RuntimeError, match="simulated failure"):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=cast(Broker, failing),
        )
    # Trigger record was persisted; no broker order was created.
    trigger_jsons = [p for p in _trigger_files(tmp_path) if p.suffix == ".json"]
    assert len(trigger_jsons) == 1
    assert _order_files(tmp_path) == []
    # Restart with a healthy broker; recovers persisted trigger.
    healthy = MockBroker(store_root=tmp_path)
    result = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=healthy,
    )
    assert result.order is not None
    assert result.recovered_from_persisted_trigger is True
    assert len(_order_files(tmp_path)) == 1
    assert len(
        [p for p in _trigger_files(tmp_path) if p.suffix == ".json"]
    ) == 1


# ---------------------------------------------------------------------------
# 11. Different unresolved order refuses without submitting another
# ---------------------------------------------------------------------------


def test_different_unresolved_order_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    other_intent = OrderIntent(
        side="sell",
        source_open_time_utc=_ACTIVATION - timedelta(minutes=_UNIT),
        unit_minutes=_UNIT,
        signal_ts_utc=_ACTIVATION,
        requested_notional_krw=None,
        requested_qty=Qty.from_str("0.1"),
    )
    broker.submit(other_intent)
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )
    # Only the pre-existing order file — dispatch did not submit another.
    assert len(_order_files(tmp_path)) == 1


# ---------------------------------------------------------------------------
# 12. Partial + complete fills restore stopped_out_lockout=true after restart
# ---------------------------------------------------------------------------


def test_partial_fill_restores_lockout_after_restart(tmp_path: Path) -> None:
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    broker = MockBroker(store_root=tmp_path)
    r1 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert r1.order is not None
    broker.record_fill(
        r1.order.client_order_id,
        fill_qty=Qty.from_str("0.2"),
        fill_notional_krw=Money.from_str("9600000"),
    )
    # Restart with a fresh broker on the same store; dispatch recovers
    # the persisted trigger and reads the partially-filled order.
    # A real partial fill reduces the on-book position by the fill qty
    # (0.5 - 0.2 = 0.3) — the recovery path must not require the
    # original stop.qty here.
    r2 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=Qty.from_str("0.3"),
        state_dir=tmp_path,
        broker=MockBroker(store_root=tmp_path),
    )
    assert r2.order is not None
    assert r2.order.state == OrderState.PARTIALLY_FILLED
    assert r2.stopped_out_lockout is True
    assert r2.recovered_from_persisted_trigger is True


def test_complete_fill_restores_lockout_after_restart(tmp_path: Path) -> None:
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    broker = MockBroker(store_root=tmp_path)
    r1 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert r1.order is not None
    broker.record_fill(
        r1.order.client_order_id,
        fill_qty=_STOP_QTY,
        fill_notional_krw=Money.from_str("24000000"),
        complete=True,
    )
    # A complete fill zeroes the position — the recovery path must
    # still read the persisted trigger's terminal fill and lockout.
    r2 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=Qty.from_str("0"),
        state_dir=tmp_path,
        broker=MockBroker(store_root=tmp_path),
    )
    assert r2.order is not None
    assert r2.order.state == OrderState.FILLED
    assert r2.stopped_out_lockout is True


# ---------------------------------------------------------------------------
# 13. Canceled + rejected orders are not automatically replaced
# ---------------------------------------------------------------------------


def test_canceled_order_not_automatically_replaced(tmp_path: Path) -> None:
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    broker = MockBroker(store_root=tmp_path)
    r1 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert r1.order is not None
    broker.cancel(r1.order.client_order_id)
    with pytest.raises(ProtectiveOrderTerminalError):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=MockBroker(store_root=tmp_path),
        )
    assert len(_order_files(tmp_path)) == 1


def test_rejected_order_not_automatically_replaced(tmp_path: Path) -> None:
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    broker = MockBroker(store_root=tmp_path)
    r1 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert r1.order is not None
    broker.reject(r1.order.client_order_id, reason="venue-rejected")
    with pytest.raises(ProtectiveOrderTerminalError):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=MockBroker(store_root=tmp_path),
        )
    assert len(_order_files(tmp_path)) == 1


# ---------------------------------------------------------------------------
# 14. Invalid inputs fail before any broker mutation
# ---------------------------------------------------------------------------


def _valid_kwargs(broker: MockBroker, tmp_path: Path) -> dict[str, object]:
    return dict(
        observation=_later_intrabar_obs(observed_price=_STOP_PRICE),
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )


def test_naive_observed_at_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    bad = replace(obs, observed_at_utc=obs.observed_at_utc.replace(tzinfo=None))
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=bad,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


def test_naive_candle_open_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    bad = replace(
        obs, candle_open_time_utc=obs.candle_open_time_utc.replace(tzinfo=None)
    )
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=bad,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


@pytest.mark.parametrize("bad_unit", [0, -1, 60, True, False, 240.0])
def test_bad_unit_minutes_refused(tmp_path: Path, bad_unit: object) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    bad = replace(obs, unit_minutes=cast(int, bad_unit))
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=bad,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


@pytest.mark.parametrize("bad_market", ["", 123, None])
def test_bad_market_refused(tmp_path: Path, bad_market: object) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    bad = replace(obs, market=cast(str, bad_market))
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=bad,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


@pytest.mark.parametrize(
    "bad_price_dec",
    [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")],
)
def test_bad_observed_price_refused(
    tmp_path: Path, bad_price_dec: Decimal
) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=Money(bad_price_dec))
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


def test_position_less_than_stop_qty_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=_later_intrabar_obs(observed_price=_STOP_PRICE),
            stop=_stop(),
            position=Qty.from_str("0.1"),
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


@pytest.mark.parametrize(
    "bad_pos_dec",
    [Decimal("-0.1"), Decimal("NaN"), Decimal("Infinity")],
)
def test_bad_position_refused(tmp_path: Path, bad_pos_dec: Decimal) -> None:
    broker = MockBroker(store_root=tmp_path)
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=_later_intrabar_obs(observed_price=_STOP_PRICE),
            stop=_stop(),
            position=Qty(bad_pos_dec),
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []


def test_float_price_construction_refused() -> None:
    # Money constructor itself rejects float — the "no float construction"
    # invariant is enforced at the value-object boundary (D-49). This
    # spot-checks that path so a caller cannot slip a float in through
    # the observation.
    with pytest.raises(TypeError):
        Money(cast(Decimal, 48000000.0))
    with pytest.raises(TypeError):
        Qty(cast(Decimal, 0.5))


def test_observed_at_before_candle_open_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    bad = replace(
        obs, observed_at_utc=obs.candle_open_time_utc - timedelta(microseconds=1)
    )
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=bad,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []


def test_observed_at_past_close_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    close = obs.candle_open_time_utc + timedelta(minutes=obs.unit_minutes)
    bad = replace(obs, observed_at_utc=close)
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=bad,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )


def test_stop_inactive_for_candle_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    # Candle strictly BEFORE activation.
    pre_open = _ACTIVATION - timedelta(minutes=_UNIT)
    price = Money.from_str("47500000")
    obs = StopObservation(
        market=_MARKET,
        candle_open_time_utc=pre_open,
        unit_minutes=_UNIT,
        observed_at_utc=pre_open + timedelta(minutes=30),
        candle_open_price=Money.from_str("49500000"),
        observed_price=price,
    )
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )


def test_boundary_price_mismatch_refused(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    obs = StopObservation(
        market=_MARKET,
        candle_open_time_utc=_LATER_OPEN,
        unit_minutes=_UNIT,
        observed_at_utc=_LATER_OPEN,
        candle_open_price=Money.from_str("47000000"),
        observed_price=Money.from_str("46000000"),
    )
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )


# ---------------------------------------------------------------------------
# 15. Corrupt persisted records fail closed & bytes stay identical
# ---------------------------------------------------------------------------


def _first_trigger_record(tmp_path: Path) -> Path:
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    broker = MockBroker(store_root=tmp_path)
    r1 = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert r1.order is not None
    jsons = [p for p in _trigger_files(tmp_path) if p.suffix == ".json"]
    assert len(jsons) == 1
    return jsons[0]


def _fresh_dispatch(tmp_path: Path) -> ProtectiveDispatchResult:
    """Dispatch as if a fresh process: fresh broker, same state_dir."""
    return dispatch_protective_stop(
        observation=_later_intrabar_obs(observed_price=_STOP_PRICE),
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=MockBroker(store_root=tmp_path / "fresh_orders"),
    )


def test_corrupt_missing_sidecar_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    before = record.read_bytes()
    sidecar.unlink()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before
    assert _order_files(tmp_path / "fresh_orders") == []


def test_corrupt_malformed_json_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    corrupt = b"{not json"
    record.write_bytes(corrupt)
    # Rewrite sidecar so hash matches but JSON is still malformed.
    sidecar.write_text(
        f"{sha256_hex(corrupt)}  {record.name}\n", encoding="utf-8"
    )
    before = record.read_bytes()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before
    assert _order_files(tmp_path / "fresh_orders") == []


def test_corrupt_bad_schema_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    data = json.loads(record.read_bytes())
    data["schema_version"] = SCHEMA_VERSION + 1
    tampered = canonical_bytes(data)
    record.write_bytes(tampered)
    sidecar.write_text(
        f"{sha256_hex(tampered)}  {record.name}\n", encoding="utf-8"
    )
    before = record.read_bytes()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before
    assert _order_files(tmp_path / "fresh_orders") == []


def test_corrupt_sidecar_hash_mismatch_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    before_record = record.read_bytes()
    # Overwrite sidecar with a wrong hex.
    sidecar.write_text(
        f"{'0' * 64}  {record.name}\n", encoding="utf-8"
    )
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before_record
    assert _order_files(tmp_path / "fresh_orders") == []


def test_corrupt_missing_key_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    data = json.loads(record.read_bytes())
    del data["client_order_id"]
    tampered = canonical_bytes(data)
    record.write_bytes(tampered)
    sidecar.write_text(
        f"{sha256_hex(tampered)}  {record.name}\n", encoding="utf-8"
    )
    before = record.read_bytes()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before


def test_corrupt_non_string_decimal_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    data = json.loads(record.read_bytes())
    data["trigger_price"] = 48000000  # int rather than string
    tampered = canonical_bytes(data)
    record.write_bytes(tampered)
    sidecar.write_text(
        f"{sha256_hex(tampered)}  {record.name}\n", encoding="utf-8"
    )
    before = record.read_bytes()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before


def test_corrupt_market_mismatch_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    data = json.loads(record.read_bytes())
    data["market"] = "KRW-ETH"
    tampered = canonical_bytes(data)
    record.write_bytes(tampered)
    sidecar.write_text(
        f"{sha256_hex(tampered)}  {record.name}\n", encoding="utf-8"
    )
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)


# ---------------------------------------------------------------------------
# 16. Source-level: no forbidden network/credential/CLI/strategy/paper imports
# ---------------------------------------------------------------------------


_FORBIDDEN_IMPORT_PREFIXES = (
    "httpx",
    "websockets",
    "requests",
    "urllib3",
    "urllib.request",
    "aiohttp",
    "grpc",
    "jwt",
    "PyJWT",
    "socket",
    "ssl",
    "bithumb_bot.secrets",
    "bithumb_bot.cli",
    "bithumb_bot.strategy",
    "bithumb_bot.paper",
    # D1: production protective.py must not depend on the mock broker
    # implementation — deterministic identity lives in the venue-neutral
    # broker.identity module.
    "bithumb_bot.broker.mock",
)


def test_module_has_no_forbidden_imports() -> None:
    src = Path(
        __file__
    ).parent.parent.parent / "src" / "bithumb_bot" / "order_flow" / "protective.py"
    source = src.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                    assert not (
                        alias.name == prefix or alias.name.startswith(prefix + ".")
                    ), f"forbidden import {alias.name!r}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                assert not (
                    mod == prefix or mod.startswith(prefix + ".")
                ), f"forbidden from-import {mod!r}"
    # Also refuse any raw environment or credential lookups.
    for banned in ("os.environ", "os.getenv", "os.putenv"):
        assert banned not in source, f"forbidden reference {banned!r}"


# ---------------------------------------------------------------------------
# Trigger record content sanity
# ---------------------------------------------------------------------------


def test_trigger_record_shape(tmp_path: Path) -> None:
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    broker = MockBroker(store_root=tmp_path)
    result = dispatch_protective_stop(
        observation=obs,
        stop=_stop(),
        position=_STOP_QTY,
        state_dir=tmp_path,
        broker=broker,
    )
    assert result.order is not None
    (record_path,) = [
        p for p in _trigger_files(tmp_path) if p.suffix == ".json"
    ]
    payload = json.loads(record_path.read_bytes())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["reason"] == "protective_stop_intrabar"
    assert payload["market"] == _MARKET
    assert payload["stop_qty"] == str(_STOP_QTY.value)
    assert payload["trigger_price"] == str(_STOP_PRICE.value)
    assert payload["client_order_id"] == result.order.client_order_id
    assert re.fullmatch(r"[0-9a-f]{64}", payload["stop_id"])
    assert record_path.name == f"{payload['stop_id']}.json"


# ---------------------------------------------------------------------------
# 17. D1 — canonical stop_id folds numerically-equal Decimal representations
# ---------------------------------------------------------------------------


def test_stop_id_folds_numerically_equal_decimal_representations(
    tmp_path: Path,
) -> None:
    """``Decimal("0.5")`` / ``Decimal("0.50")`` / ``Decimal("5E-1")`` must
    all produce the same on-disk trigger record (same stop_id) — the
    dispatcher canonicalizes stop identity via the same helper the broker
    uses to hash intents, so trailing zeros never fork the persistence."""
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)

    def _run(qty_str: str) -> str:
        subdir = tmp_path / qty_str.replace(".", "_").replace("-", "neg")
        subdir.mkdir()
        broker = MockBroker(store_root=subdir)
        stop = ProtectiveStop(
            stop_price=_STOP_PRICE,
            qty=Qty(Decimal(qty_str)),
            activated_at_utc=_ACTIVATION,
            unit_minutes=_UNIT,
            entry_fill_price=_ENTRY_PRICE,
        )
        dispatch_protective_stop(
            observation=obs,
            stop=stop,
            position=Qty(Decimal(qty_str)),
            state_dir=subdir,
            broker=broker,
        )
        jsons = [p for p in sorted(
            (subdir / "protective_triggers").iterdir()
        ) if p.suffix == ".json"]
        assert len(jsons) == 1
        return jsons[0].stem

    a = _run("0.5")
    b = _run("0.50")
    c = _run("5E-1")
    assert a == b == c


# ---------------------------------------------------------------------------
# 18. D2 — persisted trigger + missing broker order + insufficient position
# ---------------------------------------------------------------------------


def test_persisted_trigger_missing_order_insufficient_position_refused(
    tmp_path: Path,
) -> None:
    """Crash-before-submit persisted the trigger. On restart the on-book
    position is smaller than the stop's protected qty (an unrelated
    reduction). We must refuse rather than submit an oversize sell.
    """
    obs = _later_intrabar_obs(observed_price=_STOP_PRICE)
    inner = MockBroker(store_root=tmp_path)
    failing = _RaiseOnSubmitBroker(inner)
    with pytest.raises(RuntimeError, match="simulated failure"):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=cast(Broker, failing),
        )
    # Trigger persisted, no broker order.
    assert _order_files(tmp_path) == []
    assert len(
        [p for p in _trigger_files(tmp_path) if p.suffix == ".json"]
    ) == 1
    # Restart with a smaller position — refuse without submitting.
    healthy = MockBroker(store_root=tmp_path)
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=obs,
            stop=_stop(),
            position=Qty.from_str("0.1"),
            state_dir=tmp_path,
            broker=healthy,
        )
    assert _order_files(tmp_path) == []


# ---------------------------------------------------------------------------
# 19. D3 — strict immutable trigger-record validation
# ---------------------------------------------------------------------------


def test_orphan_sidecar_without_json_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    sidecar_before = sidecar.read_bytes()
    # Remove the JSON but keep the sidecar — an operator should notice
    # the anomaly, not have it silently swept away.
    record.unlink()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert sidecar.read_bytes() == sidecar_before
    assert not record.exists()
    assert _order_files(tmp_path / "fresh_orders") == []


def test_sidecar_wrong_filename_column_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    record_before = record.read_bytes()
    hex_digest = sha256_hex(record_before)
    sidecar.write_text(
        f"{hex_digest}  wrong-name.json\n", encoding="utf-8"
    )
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == record_before
    assert _order_files(tmp_path / "fresh_orders") == []


def test_sidecar_extra_token_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    record_before = record.read_bytes()
    hex_digest = sha256_hex(record_before)
    sidecar.write_text(
        f"{hex_digest}  {record.name}  extra-token\n", encoding="utf-8"
    )
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == record_before


def test_noncanonical_json_bytes_with_matching_hash_refused(
    tmp_path: Path,
) -> None:
    """JSON with the wrong separator style still parses to the same
    dict, but its bytes are not canonical — accepting it would let two
    identical records disagree on-disk and break any downstream
    byte-equality check."""
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    data = json.loads(record.read_bytes())
    # Insignificant whitespace ⇒ non-canonical bytes with a valid parse.
    tampered = json.dumps(
        data, sort_keys=True, separators=(", ", ": "), ensure_ascii=False
    ).encode("utf-8") + b"\n"
    record.write_bytes(tampered)
    sidecar.write_text(
        f"{sha256_hex(tampered)}  {record.name}\n", encoding="utf-8"
    )
    before = record.read_bytes()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before


def test_unexpected_json_key_refused(tmp_path: Path) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    data = json.loads(record.read_bytes())
    data["unexpected"] = "surprise"
    tampered = canonical_bytes(data)
    record.write_bytes(tampered)
    sidecar.write_text(
        f"{sha256_hex(tampered)}  {record.name}\n", encoding="utf-8"
    )
    before = record.read_bytes()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before


def test_changed_stop_qty_with_recomputed_sidecar_refused(
    tmp_path: Path,
) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    data = json.loads(record.read_bytes())
    # Numerically different from the current stop.qty (0.5).
    data["stop_qty"] = "0.4"
    tampered = canonical_bytes(data)
    record.write_bytes(tampered)
    sidecar.write_text(
        f"{sha256_hex(tampered)}  {record.name}\n", encoding="utf-8"
    )
    before = record.read_bytes()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before


def test_changed_unit_minutes_with_recomputed_sidecar_refused(
    tmp_path: Path,
) -> None:
    record = _first_trigger_record(tmp_path)
    sidecar = record.with_name(f"{record.name}.sha256")
    data = json.loads(record.read_bytes())
    data["unit_minutes"] = _UNIT + 60
    tampered = canonical_bytes(data)
    record.write_bytes(tampered)
    sidecar.write_text(
        f"{sha256_hex(tampered)}  {record.name}\n", encoding="utf-8"
    )
    before = record.read_bytes()
    with pytest.raises(ProtectiveTriggerRecordCorrupt):
        _fresh_dispatch(tmp_path)
    assert record.read_bytes() == before


# ---------------------------------------------------------------------------
# 20. D4 — stop.unit_minutes bool refused before any mutation
# ---------------------------------------------------------------------------


def test_stop_unit_minutes_bool_refused_before_any_mutation(
    tmp_path: Path,
) -> None:
    """``ProtectiveStop(unit_minutes=True)`` numerically equals 1 (so
    matches ``observation.unit_minutes=1``) but must still be refused —
    a bool leaking through would corrupt every persisted record and
    every hashed intent that includes it."""
    broker = MockBroker(store_root=tmp_path)
    stop = ProtectiveStop(
        stop_price=_STOP_PRICE,
        qty=_STOP_QTY,
        activated_at_utc=_ACTIVATION,
        unit_minutes=cast(int, True),
        entry_fill_price=_ENTRY_PRICE,
    )
    obs = StopObservation(
        market=_MARKET,
        candle_open_time_utc=_LATER_OPEN,
        unit_minutes=1,
        observed_at_utc=_LATER_OPEN,
        candle_open_price=Money.from_str("47000000"),
        observed_price=Money.from_str("47000000"),
    )
    with pytest.raises(ProtectiveDispatchRefused):
        dispatch_protective_stop(
            observation=obs,
            stop=stop,
            position=_STOP_QTY,
            state_dir=tmp_path,
            broker=broker,
        )
    assert _order_files(tmp_path) == []
    assert _trigger_files(tmp_path) == []
