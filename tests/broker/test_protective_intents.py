"""MockBroker persistence tests for the new protective-stop fields.

Covers only the scaffold added by this patch:

* Round-trip of ``reason`` and ``trigger_price`` through submit → disk.
* Restart of a fresh MockBroker reads the same values back.
* Bumped schema (v1 → v2): any v1 payload fails closed on load.
* On-disk corruption of the new fields (bad reason, non-positive
  trigger_price, missing key, inconsistent timing / side combos) raises
  ``BrokerStateCorrupt`` and never modifies the file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.broker.mock import (
    SCHEMA_VERSION,
    BrokerStateCorrupt,
    MockBroker,
)
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent

BASE_OPEN = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
UNIT = 240
CLOSE = BASE_OPEN + timedelta(minutes=UNIT)


def _gap_intent() -> OrderIntent:
    return OrderIntent.protective_sell(
        reason="protective_stop_gap",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=BASE_OPEN,
        trigger_price=Money.from_str("48000000"),
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
    )


def _intrabar_intent() -> OrderIntent:
    return OrderIntent.protective_sell(
        reason="protective_stop_intrabar",
        qty=Qty.from_str("0.5"),
        trigger_ts_utc=BASE_OPEN + timedelta(minutes=90),
        trigger_price=Money.from_str("48123456.789"),
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
    )


def _strategy_intent() -> OrderIntent:
    return OrderIntent(
        side="buy",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=UNIT,
        signal_ts_utc=CLOSE,
        requested_notional_krw=Money.from_str("100000"),
        requested_qty=None,
    )


def _rewrite(path: Path, mutator: object) -> bytes:
    before = path.read_bytes()
    data = json.loads(before.decode("utf-8"))
    if callable(mutator):
        result = mutator(data)
        if result is not None:
            data = result
    after = json.dumps(data).encode("utf-8")
    path.write_bytes(after)
    return after


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_gap_intent_persists_reason_and_trigger_price(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    intent = _gap_intent()
    order = broker.submit(intent)

    disk = json.loads(
        (tmp_path / "orders" / f"{order.client_order_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert disk["schema_version"] == SCHEMA_VERSION == 2
    assert disk["intent"]["reason"] == "protective_stop_gap"
    assert disk["intent"]["trigger_price"] == "48000000"


def test_strategy_intent_persists_default_reason_and_null_trigger(
    tmp_path: Path,
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    disk = json.loads(
        (tmp_path / "orders" / f"{order.client_order_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert disk["intent"]["reason"] == "strategy_signal"
    assert disk["intent"]["trigger_price"] is None


def test_intrabar_intent_restart_reloads_every_new_field(tmp_path: Path) -> None:
    intent = _intrabar_intent()
    b1 = MockBroker(store_root=tmp_path)
    first = b1.submit(intent)
    del b1

    b2 = MockBroker(store_root=tmp_path)
    reloaded = b2.get(first.client_order_id)
    assert reloaded is not None
    assert reloaded.intent.reason == "protective_stop_intrabar"
    assert reloaded.intent.trigger_price is not None
    assert reloaded.intent.trigger_price.value == Decimal("48123456.789")
    assert reloaded.intent.signal_ts_utc == BASE_OPEN + timedelta(minutes=90)
    assert reloaded.intent.source_open_time_utc == BASE_OPEN

    # Idempotency: resubmitting the same intent returns the same order.
    second = b2.submit(intent)
    assert second.client_order_id == first.client_order_id


# ---------------------------------------------------------------------------
# Old schema — must fail closed
# ---------------------------------------------------------------------------


def test_schema_v1_payload_fails_closed(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def downgrade(d: dict[str, object]) -> None:
        d["schema_version"] = 1
        # v1 payloads lacked reason/trigger_price entirely
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent.pop("reason", None)
        intent.pop("trigger_price", None)

    after = _rewrite(path, downgrade)
    with pytest.raises(BrokerStateCorrupt, match="schema_version"):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


# ---------------------------------------------------------------------------
# Corrupt / inconsistent combinations — fail closed, no mutation
# ---------------------------------------------------------------------------


def test_missing_reason_key_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        del intent["reason"]

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt, match="reason"):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_missing_trigger_price_key_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_gap_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        del intent["trigger_price"]

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt, match="trigger_price"):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_unknown_reason_string_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["reason"] = "made-up"

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_strategy_signal_with_trigger_price_on_disk_is_corrupt(
    tmp_path: Path,
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["trigger_price"] = "48000000"

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_protective_missing_trigger_price_value_is_corrupt(
    tmp_path: Path,
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_gap_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["trigger_price"] = None

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_trigger_price_zero_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_gap_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["trigger_price"] = "0"

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt, match="trigger_price"):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_trigger_price_infinity_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_gap_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["trigger_price"] = "Infinity"

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_protective_wrong_side_on_disk_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_gap_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["side"] = "buy"
        intent["requested_notional_krw"] = "100000"
        intent["requested_qty"] = None

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_persisted_unit_minutes_zero_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["unit_minutes"] = 0

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_persisted_unit_minutes_negative_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["unit_minutes"] = -240

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_persisted_unit_minutes_bool_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["unit_minutes"] = True

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_persisted_naive_timestamp_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        # Strip tzinfo from source_open_time_utc string
        intent["source_open_time_utc"] = "2026-01-01T00:00:00"

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_persisted_buy_notional_nan_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_strategy_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["requested_notional_krw"] = "NaN"

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after


def test_protective_gap_wrong_timing_on_disk_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_gap_intent())
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        # Push signal_ts one minute past source open — no longer a valid
        # gap-reason payload.
        intent["signal_ts_utc"] = (BASE_OPEN + timedelta(minutes=1)).isoformat()

    after = _rewrite(path, mutate)
    with pytest.raises(BrokerStateCorrupt):
        broker.get(order.client_order_id)
    assert path.read_bytes() == after
