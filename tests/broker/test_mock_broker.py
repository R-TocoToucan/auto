"""Focused functional tests for the persistent, idempotent MockBroker.

Every scenario named in the M6A precursor spec is covered exactly once
here, hand-verifiable, no fixtures beyond ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.broker.mock import (
    BrokerStateCorrupt,
    InvalidStateTransition,
    MockBroker,
    deterministic_client_order_id,
)
from bithumb_bot.broker.state import OrderState
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent

BASE_OPEN = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _buy_intent(notional: str = "100000") -> OrderIntent:
    return OrderIntent(
        side="buy",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=240,
        signal_ts_utc=BASE_OPEN + timedelta(minutes=240),
        requested_notional_krw=Money.from_str(notional),
        requested_qty=None,
    )


def _sell_intent(qty: str = "0.5") -> OrderIntent:
    return OrderIntent(
        side="sell",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=240,
        signal_ts_utc=BASE_OPEN + timedelta(minutes=240),
        requested_notional_krw=None,
        requested_qty=Qty.from_str(qty),
    )


def _order_files(root: Path) -> list[Path]:
    return sorted((root / "orders").glob("*.json"))


def test_first_submission_creates_exactly_one_order(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    intent = _buy_intent()
    order = broker.submit(intent)
    assert order.state == OrderState.ACCEPTED
    files = _order_files(tmp_path)
    assert len(files) == 1
    assert files[0].stem == order.client_order_id


def test_duplicate_submission_returns_same_order(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    intent = _buy_intent()
    first = broker.submit(intent)
    second = broker.submit(intent)
    assert first.client_order_id == second.client_order_id
    assert first == second
    assert len(_order_files(tmp_path)) == 1


def test_restart_then_retry_creates_no_duplicate(tmp_path: Path) -> None:
    intent = _buy_intent()
    b1 = MockBroker(store_root=tmp_path)
    first = b1.submit(intent)
    del b1
    b2 = MockBroker(store_root=tmp_path)
    second = b2.submit(intent)
    assert second.client_order_id == first.client_order_id
    assert second.state == OrderState.ACCEPTED
    assert len(_order_files(tmp_path)) == 1


def test_client_order_id_covers_every_intent_field() -> None:
    base = _buy_intent("100000")
    same = _buy_intent("100000")
    assert deterministic_client_order_id(base) == deterministic_client_order_id(same)
    assert (
        deterministic_client_order_id(base)
        != deterministic_client_order_id(_buy_intent("100001"))
    )
    assert (
        deterministic_client_order_id(base)
        != deterministic_client_order_id(_sell_intent())
    )
    later_open = BASE_OPEN + timedelta(hours=4)
    later = OrderIntent(
        side="buy",
        source_open_time_utc=later_open,
        unit_minutes=240,
        signal_ts_utc=later_open + timedelta(minutes=240),
        requested_notional_krw=Money.from_str("100000"),
        requested_qty=None,
    )
    assert deterministic_client_order_id(base) != deterministic_client_order_id(later)
    other_unit = OrderIntent(
        side="buy",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=60,
        signal_ts_utc=BASE_OPEN + timedelta(minutes=60),
        requested_notional_krw=Money.from_str("100000"),
        requested_qty=None,
    )
    assert (
        deterministic_client_order_id(base)
        != deterministic_client_order_id(other_unit)
    )


def test_accepted_partial_filled_progression(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell_intent("1.0"))
    assert order.state == OrderState.ACCEPTED

    partial = broker.record_fill(
        order.client_order_id,
        Qty.from_str("0.4"),
        Money.from_str("40000"),
    )
    assert partial.state == OrderState.PARTIALLY_FILLED
    assert partial.filled_qty.value == Decimal("0.4")
    assert partial.filled_notional_krw.value == Decimal("40000")

    partial2 = broker.record_fill(
        order.client_order_id,
        Qty.from_str("0.1"),
        Money.from_str("10000"),
    )
    assert partial2.state == OrderState.PARTIALLY_FILLED
    assert partial2.filled_qty.value == Decimal("0.5")

    final = broker.record_fill(
        order.client_order_id,
        Qty.from_str("0.5"),
        Money.from_str("50000"),
        complete=True,
    )
    assert final.state == OrderState.FILLED
    assert final.filled_qty.value == Decimal("1.0")
    assert final.filled_notional_krw.value == Decimal("100000")


def test_cancel_from_accepted(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy_intent())
    canceled = broker.cancel(order.client_order_id)
    assert canceled.state == OrderState.CANCELED


def test_cancel_from_partial_preserves_fill_totals(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell_intent("1.0"))
    broker.record_fill(
        order.client_order_id,
        Qty.from_str("0.2"),
        Money.from_str("20000"),
    )
    canceled = broker.cancel(order.client_order_id)
    assert canceled.state == OrderState.CANCELED
    assert canceled.filled_qty.value == Decimal("0.2")
    assert canceled.filled_notional_krw.value == Decimal("20000")


def test_rejected_order_cannot_fill(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy_intent())
    rejected = broker.reject(order.client_order_id, reason="min-notional")
    assert rejected.state == OrderState.REJECTED
    assert rejected.rejection_reason == "min-notional"
    with pytest.raises(InvalidStateTransition):
        broker.record_fill(
            order.client_order_id,
            Qty.from_str("0.1"),
            Money.from_str("1000"),
        )


def test_filled_canceled_rejected_are_terminal(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)

    filled_order = broker.submit(_sell_intent("1.0"))
    broker.record_fill(
        filled_order.client_order_id,
        Qty.from_str("1.0"),
        Money.from_str("100000"),
        complete=True,
    )
    with pytest.raises(InvalidStateTransition):
        broker.cancel(filled_order.client_order_id)
    with pytest.raises(InvalidStateTransition):
        broker.record_fill(
            filled_order.client_order_id,
            Qty.from_str("0.1"),
            Money.from_str("1000"),
        )
    with pytest.raises(InvalidStateTransition):
        broker.reject(filled_order.client_order_id, reason="late")

    canceled_order = broker.submit(_buy_intent("50000"))
    broker.cancel(canceled_order.client_order_id)
    with pytest.raises(InvalidStateTransition):
        broker.record_fill(
            canceled_order.client_order_id,
            Qty.from_str("0.1"),
            Money.from_str("1000"),
        )
    with pytest.raises(InvalidStateTransition):
        broker.reject(canceled_order.client_order_id, reason="late")
    with pytest.raises(InvalidStateTransition):
        broker.cancel(canceled_order.client_order_id)

    rejected_order = broker.submit(_buy_intent("60000"))
    broker.reject(rejected_order.client_order_id, reason="min-notional")
    with pytest.raises(InvalidStateTransition):
        broker.cancel(rejected_order.client_order_id)
    with pytest.raises(InvalidStateTransition):
        broker.reject(rejected_order.client_order_id, reason="again")
    with pytest.raises(InvalidStateTransition):
        broker.record_fill(
            rejected_order.client_order_id,
            Qty.from_str("0.1"),
            Money.from_str("1000"),
        )


def test_query_by_client_order_id_and_list_open(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    a = broker.submit(_buy_intent("50000"))
    b = broker.submit(_sell_intent("0.3"))
    c = broker.submit(_buy_intent("70000"))
    broker.cancel(c.client_order_id)

    assert broker.get(a.client_order_id) == a
    assert broker.get(b.client_order_id) == b
    assert broker.get("0" * 64) is None
    assert broker.get("not-a-hex-string") is None

    open_ids = {o.client_order_id for o in broker.list_open()}
    assert open_ids == {a.client_order_id, b.client_order_id}


def test_malformed_json_fails_without_mutation(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    corrupt_bytes = b"{not valid json"
    path.write_bytes(corrupt_bytes)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    with pytest.raises(BrokerStateCorrupt):
        broker.submit(_buy_intent())
    with pytest.raises(BrokerStateCorrupt):
        broker.list_open()
    assert path.read_bytes() == corrupt_bytes
    assert _order_files(tmp_path) == [path]


def test_unknown_state_string_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state"] = "made-up-state"
    payload = json.dumps(data)
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_text(encoding="utf-8") == payload


def test_persisted_intent_tamper_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy_intent("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["intent"]["requested_notional_krw"] = "999999"
    payload = json.dumps(data)
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_text(encoding="utf-8") == payload


def test_decimal_roundtrip_is_exact(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    tricky = "12345678901234567890.1234567890123456789"
    intent = OrderIntent(
        side="buy",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=240,
        signal_ts_utc=BASE_OPEN + timedelta(minutes=240),
        requested_notional_krw=Money.from_str(tricky),
        requested_qty=None,
    )
    order = broker.submit(intent)

    disk = json.loads(
        (tmp_path / "orders" / f"{order.client_order_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(disk["intent"]["requested_notional_krw"], str)
    assert disk["intent"]["requested_notional_krw"] == tricky

    reloaded_broker = MockBroker(store_root=tmp_path)
    reloaded = reloaded_broker.get(order.client_order_id)
    assert reloaded is not None
    assert reloaded.intent.requested_notional_krw is not None
    assert reloaded.intent.requested_notional_krw.value == Decimal(tricky)

    reloaded_broker.record_fill(
        order.client_order_id,
        Qty.from_str("0.12345678"),
        Money.from_str("999999.999999"),
    )
    disk2 = json.loads(
        (tmp_path / "orders" / f"{order.client_order_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(disk2["filled_qty"], str)
    assert isinstance(disk2["filled_notional_krw"], str)
    final_broker = MockBroker(store_root=tmp_path)
    final = final_broker.get(order.client_order_id)
    assert final is not None
    assert final.filled_qty.value == Decimal("0.12345678")
    assert final.filled_notional_krw.value == Decimal("999999.999999")
