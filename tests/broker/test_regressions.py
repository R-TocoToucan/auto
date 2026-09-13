"""Regression tests for the second-pass MockBroker defects.

Each test names the defect it guards. Every "no mutation" assertion
snapshots the file bytes before the failing call and verifies they are
unchanged after.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.broker.mock import (
    BrokerStateCorrupt,
    FillExceedsIntent,
    MockBroker,
    deterministic_client_order_id,
)
from bithumb_bot.broker.state import OrderState
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent

BASE_OPEN_UTC = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _buy(notional: str, source_open: datetime = BASE_OPEN_UTC) -> OrderIntent:
    return OrderIntent(
        side="buy",
        source_open_time_utc=source_open,
        unit_minutes=240,
        signal_ts_utc=source_open + timedelta(minutes=240),
        requested_notional_krw=Money.from_str(notional),
        requested_qty=None,
    )


def _sell(qty: str, source_open: datetime = BASE_OPEN_UTC) -> OrderIntent:
    return OrderIntent(
        side="sell",
        source_open_time_utc=source_open,
        unit_minutes=240,
        signal_ts_utc=source_open + timedelta(minutes=240),
        requested_notional_krw=None,
        requested_qty=Qty.from_str(qty),
    )


# ---------------------------------------------------------------------------
# 1. Canonical client_order_id
# ---------------------------------------------------------------------------


def test_client_order_id_normalizes_utc_offset() -> None:
    kst = timezone(timedelta(hours=9))
    same_instant_kst = BASE_OPEN_UTC.astimezone(kst)
    intent_utc = _buy("100000", source_open=BASE_OPEN_UTC)
    intent_kst = _buy("100000", source_open=same_instant_kst)
    assert deterministic_client_order_id(intent_utc) == deterministic_client_order_id(
        intent_kst
    )


def test_client_order_id_normalizes_decimal_trailing_zeros() -> None:
    a = _buy("100000")
    b = _buy("100000.00")
    c = _buy("100000.000000")
    cid = deterministic_client_order_id(a)
    assert deterministic_client_order_id(b) == cid
    assert deterministic_client_order_id(c) == cid


def test_client_order_id_preserves_high_precision() -> None:
    """Distinct arbitrary-precision Decimals must never collide.

    The two values differ only in the 19th fractional digit — well past
    the 28-digit default Decimal context precision. A ``.normalize()``-based
    canonicalizer would round both to the same 28-significant-digit
    value and collide their client_order_ids; the as_tuple()-based
    canonicalizer must not.
    """
    a = _buy("12345678901234567890.1234567890123456789")
    b = _buy("12345678901234567890.1234567890123456790")
    assert deterministic_client_order_id(a) != deterministic_client_order_id(b)


def test_canonical_decimal_folds_signed_and_scaled_zeros() -> None:
    """Every representation of zero must collapse to a single string.

    OrderIntent forbids zero-request amounts, so this validates the
    canonical helper directly. Signed zero, trailing-zero-fraction, and
    high-exponent zero forms must all fold together.
    """
    from bithumb_bot.broker.mock import _canonical_decimal  # noqa: PLC0415

    canonical_zero = _canonical_decimal(Decimal("0"))
    assert _canonical_decimal(Decimal("-0")) == canonical_zero
    assert _canonical_decimal(Decimal("0.00")) == canonical_zero
    assert _canonical_decimal(Decimal("0E+3")) == canonical_zero
    assert _canonical_decimal(Decimal("-0.000")) == canonical_zero


def test_client_order_id_rejects_naive_timestamp() -> None:
    naive_open = datetime(2026, 1, 1, 0, 0)  # no tzinfo
    intent = OrderIntent(
        side="buy",
        source_open_time_utc=naive_open,
        unit_minutes=240,
        signal_ts_utc=naive_open + timedelta(minutes=240),
        requested_notional_krw=Money.from_str("100000"),
        requested_qty=None,
    )
    with pytest.raises(ValueError, match="naive datetime"):
        deterministic_client_order_id(intent)


def test_client_order_id_rejects_non_finite_decimal() -> None:
    # OrderIntent's own positivity check catches -Infinity/NaN before the
    # canonical hasher sees them; +Infinity satisfies "> 0" so it is the
    # case where the canonical function's fail-closed guard actually fires.
    inf_intent = OrderIntent(
        side="buy",
        source_open_time_utc=BASE_OPEN_UTC,
        unit_minutes=240,
        signal_ts_utc=BASE_OPEN_UTC + timedelta(minutes=240),
        requested_notional_krw=Money(Decimal("Infinity")),
        requested_qty=None,
    )
    with pytest.raises(ValueError, match="non-finite"):
        deterministic_client_order_id(inf_intent)


def test_submit_with_naive_timestamp_fails_before_any_write(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    naive_open = datetime(2026, 1, 1, 0, 0)
    bad = OrderIntent(
        side="buy",
        source_open_time_utc=naive_open,
        unit_minutes=240,
        signal_ts_utc=naive_open + timedelta(minutes=240),
        requested_notional_krw=Money.from_str("100000"),
        requested_qty=None,
    )
    with pytest.raises(ValueError):
        broker.submit(bad)
    assert list((tmp_path / "orders").glob("*.json")) == []


def test_submit_two_offsets_returns_same_order(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    kst = timezone(timedelta(hours=9))
    intent_a = _buy("100000", source_open=BASE_OPEN_UTC)
    intent_b = _buy("100000", source_open=BASE_OPEN_UTC.astimezone(kst))
    order_a = broker.submit(intent_a)
    order_b = broker.submit(intent_b)
    assert order_a.client_order_id == order_b.client_order_id
    assert len(list((tmp_path / "orders").glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# 2. Fill bounds
# ---------------------------------------------------------------------------


def test_sell_cumulative_qty_cannot_exceed_requested(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    broker.record_fill(
        order.client_order_id, Qty.from_str("0.7"), Money.from_str("70000")
    )
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(FillExceedsIntent):
        broker.record_fill(
            order.client_order_id, Qty.from_str("0.4"), Money.from_str("40000")
        )
    assert path.read_bytes() == before


def test_sell_complete_flag_cannot_overshoot(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(FillExceedsIntent):
        broker.record_fill(
            order.client_order_id,
            Qty.from_str("1.5"),
            Money.from_str("150000"),
            complete=True,
        )
    assert path.read_bytes() == before


def test_buy_cumulative_notional_cannot_exceed_requested(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    broker.record_fill(
        order.client_order_id, Qty.from_str("0.6"), Money.from_str("60000")
    )
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(FillExceedsIntent):
        broker.record_fill(
            order.client_order_id, Qty.from_str("0.5"), Money.from_str("50000")
        )
    assert path.read_bytes() == before


def test_fill_exactly_at_bound_is_accepted(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    partial = broker.record_fill(
        order.client_order_id, Qty.from_str("0.4"), Money.from_str("40000")
    )
    assert partial.state == OrderState.PARTIALLY_FILLED
    finished = broker.record_fill(
        order.client_order_id,
        Qty.from_str("0.6"),
        Money.from_str("60000"),
        complete=True,
    )
    assert finished.state == OrderState.FILLED
    assert finished.filled_qty.value == Decimal("1.0")


# ---------------------------------------------------------------------------
# 3. Persisted history validation
# ---------------------------------------------------------------------------


def _rewrite_json(path: Path, mutator: object) -> tuple[bytes, bytes]:
    """Apply ``mutator`` (dict -> None or dict) to path; return (before, after)."""
    before = path.read_bytes()
    data = json.loads(before.decode("utf-8"))
    if callable(mutator):
        result = mutator(data)
        if result is not None:
            data = result
    after = json.dumps(data).encode("utf-8")
    path.write_bytes(after)
    return before, after


def _corrupt(broker: MockBroker, cid: str, path: Path) -> None:
    """After a tampered write, both get() and list_open() must raise."""
    with pytest.raises(BrokerStateCorrupt):
        broker.get(cid)
    with pytest.raises(BrokerStateCorrupt):
        broker.list_open()


def test_history_first_entry_must_be_none_to_accepted(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    _, after = _rewrite_json(
        path, lambda d: d.__setitem__("history", [
            {"from_state": "accepted", "to_state": "accepted",
             "at_utc": d["history"][0]["at_utc"]}
        ])
    )
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_history_chain_from_state_mismatch(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    broker.record_fill(
        order.client_order_id, Qty.from_str("0.3"), Money.from_str("30000")
    )
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        hist = d["history"]
        assert isinstance(hist, list)
        # second entry: change from_state to something != previous.to_state
        hist[1]["from_state"] = "filled"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_history_disallowed_transition(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        hist = d["history"]
        assert isinstance(hist, list)
        # append illegal accepted -> accepted transition
        hist.append(
            {
                "from_state": "accepted",
                "to_state": "accepted",
                "at_utc": hist[0]["at_utc"],
            }
        )
        # keep top-level state consistent with the tampered tail so the
        # "final state matches" check is not what triggers the error —
        # the transition-permitted check must fire first.
        d["state"] = "accepted"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_history_timestamps_must_be_nondecreasing(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    broker.record_fill(
        order.client_order_id, Qty.from_str("0.3"), Money.from_str("30000")
    )
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        hist = d["history"]
        assert isinstance(hist, list)
        # push the second entry's timestamp before the first
        hist[1]["at_utc"] = "2020-01-01T00:00:00+00:00"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_history_timestamps_must_be_tz_aware(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        hist = d["history"]
        assert isinstance(hist, list)
        hist[0]["at_utc"] = "2026-01-01T00:00:00"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_history_final_state_must_match_order_state(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        d["state"] = "canceled"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


# ---------------------------------------------------------------------------
# 4. list_open fail-closed on unexpected filename
# ---------------------------------------------------------------------------


def test_list_open_fails_closed_on_unexpected_filename(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    broker.submit(_buy("100000"))
    (tmp_path / "orders" / "garbage.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BrokerStateCorrupt, match="unexpected filename"):
        broker.list_open()


# ---------------------------------------------------------------------------
# 5. Persisted Decimal validation
# ---------------------------------------------------------------------------


def test_filled_qty_must_be_string(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    _, after = _rewrite_json(path, lambda d: d.__setitem__("filled_qty", 0))
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_filled_notional_must_be_string(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    _, after = _rewrite_json(path, lambda d: d.__setitem__("filled_notional_krw", 0.0))
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_filled_qty_infinity_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    _, after = _rewrite_json(
        path, lambda d: d.__setitem__("filled_qty", "Infinity")
    )
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_filled_notional_nan_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    _, after = _rewrite_json(
        path, lambda d: d.__setitem__("filled_notional_krw", "NaN")
    )
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_filled_qty_negative_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    _, after = _rewrite_json(
        path, lambda d: d.__setitem__("filled_qty", "-0.001")
    )
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_requested_notional_zero_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["requested_notional_krw"] = "0"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_requested_qty_negative_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["requested_qty"] = "-1"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_requested_notional_garbage_string_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["requested_notional_krw"] = "not-a-number"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_requested_notional_infinity_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        intent = d["intent"]
        assert isinstance(intent, dict)
        intent["requested_notional_krw"] = "Infinity"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


# ---------------------------------------------------------------------------
# Load-time fill bounds and state-consistency invariants
# ---------------------------------------------------------------------------


def _submit_partial(
    broker: MockBroker, intent: OrderIntent, qty: str, notional: str
) -> str:
    order = broker.submit(intent)
    broker.record_fill(
        order.client_order_id, Qty.from_str(qty), Money.from_str(notional)
    )
    return order.client_order_id


def test_load_time_sell_qty_bound_violation_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    cid = _submit_partial(broker, _sell("1.0"), "0.4", "40000")
    path = tmp_path / "orders" / f"{cid}.json"

    def mutate(d: dict[str, object]) -> None:
        d["filled_qty"] = "1.5"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, cid, path)
    assert path.read_bytes() == after


def test_load_time_buy_notional_bound_violation_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    cid = _submit_partial(broker, _buy("100000"), "0.4", "40000")
    path = tmp_path / "orders" / f"{cid}.json"

    def mutate(d: dict[str, object]) -> None:
        d["filled_notional_krw"] = "150000"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, cid, path)
    assert path.read_bytes() == after


def test_accepted_with_positive_fills_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        d["filled_qty"] = "0.1"
        d["filled_notional_krw"] = "10000"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_rejected_with_positive_fills_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    broker.reject(order.client_order_id, reason="min-notional")
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        d["filled_qty"] = "0.01"
        d["filled_notional_krw"] = "1000"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_rejected_requires_non_empty_rejection_reason(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    broker.reject(order.client_order_id, reason="min-notional")
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        d["rejection_reason"] = None

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_non_rejected_state_requires_null_rejection_reason(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"

    def mutate(d: dict[str, object]) -> None:
        d["rejection_reason"] = "should-not-be-set"

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_partially_filled_with_zero_totals_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    original_at = json.loads(path.read_text(encoding="utf-8"))["history"][0]["at_utc"]

    def mutate(d: dict[str, object]) -> None:
        d["state"] = "partially_filled"
        d["history"] = [
            {
                "from_state": None,
                "to_state": "accepted",
                "at_utc": original_at,
            },
            {
                "from_state": "accepted",
                "to_state": "partially_filled",
                "at_utc": original_at,
            },
        ]

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_filled_with_zero_totals_is_corrupt(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    original_at = json.loads(path.read_text(encoding="utf-8"))["history"][0]["at_utc"]

    def mutate(d: dict[str, object]) -> None:
        d["state"] = "filled"
        d["history"] = [
            {
                "from_state": None,
                "to_state": "accepted",
                "at_utc": original_at,
            },
            {
                "from_state": "accepted",
                "to_state": "filled",
                "at_utc": original_at,
            },
        ]

    _, after = _rewrite_json(path, mutate)
    _corrupt(broker, order.client_order_id, path)
    assert path.read_bytes() == after


def test_canceled_with_positive_fills_reloads_cleanly(tmp_path: Path) -> None:
    """The one 'either/or' state — canceled tolerates zero or positive."""
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    broker.record_fill(
        order.client_order_id, Qty.from_str("0.3"), Money.from_str("30000")
    )
    broker.cancel(order.client_order_id)

    fresh = MockBroker(store_root=tmp_path)
    reloaded = fresh.get(order.client_order_id)
    assert reloaded is not None
    assert reloaded.state == OrderState.CANCELED
    assert reloaded.filled_qty.value == Decimal("0.3")
    assert reloaded.filled_notional_krw.value == Decimal("30000")


def test_canceled_from_accepted_with_zero_fills_reloads_cleanly(
    tmp_path: Path,
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    broker.cancel(order.client_order_id)

    fresh = MockBroker(store_root=tmp_path)
    reloaded = fresh.get(order.client_order_id)
    assert reloaded is not None
    assert reloaded.state == OrderState.CANCELED
    assert reloaded.filled_qty.value == Decimal("0")
    assert reloaded.filled_notional_krw.value == Decimal("0")
