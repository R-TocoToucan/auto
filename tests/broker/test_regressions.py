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
    BrokerClockError,
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


def test_naive_timestamp_rejected_at_construction() -> None:
    # OrderIntent's __post_init__ now refuses naive timestamps universally,
    # so the invariant fires before any hasher/submit boundary can see them.
    naive_open = datetime(2026, 1, 1, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        OrderIntent(
            side="buy",
            source_open_time_utc=naive_open,
            unit_minutes=240,
            signal_ts_utc=naive_open + timedelta(minutes=240),
            requested_notional_krw=Money.from_str("100000"),
            requested_qty=None,
        )


def test_non_finite_decimal_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="finite"):
        OrderIntent(
            side="buy",
            source_open_time_utc=BASE_OPEN_UTC,
            unit_minutes=240,
            signal_ts_utc=BASE_OPEN_UTC + timedelta(minutes=240),
            requested_notional_krw=Money(Decimal("Infinity")),
            requested_qty=None,
        )


def test_submit_with_naive_timestamp_fails_before_any_write(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    naive_open = datetime(2026, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        OrderIntent(
            side="buy",
            source_open_time_utc=naive_open,
            unit_minutes=240,
            signal_ts_utc=naive_open + timedelta(minutes=240),
            requested_notional_krw=Money.from_str("100000"),
            requested_qty=None,
        )
    # The broker sees nothing: construction fails before submit is reached.
    assert list((tmp_path / "orders").glob("*.json")) == []
    _ = broker  # keep the mock broker referenced so lint is happy


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


# ---------------------------------------------------------------------------
# record_fill / reject argument validation — refuse BEFORE any mutation
# ---------------------------------------------------------------------------


def test_record_fill_infinite_qty_refuses_without_mutation(tmp_path: Path) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="finite"):
        broker.record_fill(
            order.client_order_id,
            Qty(Decimal("Infinity")),
            Money.from_str("100"),
        )
    assert path.read_bytes() == before
    # Reload verifies the on-disk file is still valid schema-v2.
    reloaded = MockBroker(store_root=tmp_path).get(order.client_order_id)
    assert reloaded is not None
    assert reloaded.state == OrderState.ACCEPTED


def test_record_fill_infinite_notional_refuses_without_mutation(
    tmp_path: Path,
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="finite"):
        broker.record_fill(
            order.client_order_id,
            Qty.from_str("0.1"),
            Money(Decimal("Infinity")),
        )
    assert path.read_bytes() == before
    reloaded = MockBroker(store_root=tmp_path).get(order.client_order_id)
    assert reloaded is not None


@pytest.mark.parametrize(
    "qty_value",
    [Decimal("NaN"), Decimal("0"), Decimal("-0.001")],
)
def test_record_fill_nan_zero_negative_qty_refuses_cleanly(
    tmp_path: Path, qty_value: Decimal
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        broker.record_fill(
            order.client_order_id,
            Qty(qty_value),
            Money.from_str("100"),
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "notional_value",
    [Decimal("NaN"), Decimal("0"), Decimal("-1")],
)
def test_record_fill_nan_zero_negative_notional_refuses_cleanly(
    tmp_path: Path, notional_value: Decimal
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        broker.record_fill(
            order.client_order_id,
            Qty.from_str("0.0001"),
            Money(notional_value),
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad_complete", [0, 1, "true", None])
def test_record_fill_non_bool_complete_refuses(
    tmp_path: Path, bad_complete: object
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_sell("1.0"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="bool"):
        broker.record_fill(
            order.client_order_id,
            Qty.from_str("0.1"),
            Money.from_str("10000"),
            complete=bad_complete,  # type: ignore[arg-type]
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "bad_reason",
    [None, 0, 1, "", "   ", "\t\n "],
)
def test_reject_non_string_or_blank_reason_refuses(
    tmp_path: Path, bad_reason: object
) -> None:
    broker = MockBroker(store_root=tmp_path)
    order = broker.submit(_buy("100000"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        broker.reject(order.client_order_id, reason=bad_reason)  # type: ignore[arg-type]
    assert path.read_bytes() == before
    # Order stays ACCEPTED and reloadable.
    reloaded = MockBroker(store_root=tmp_path).get(order.client_order_id)
    assert reloaded is not None
    assert reloaded.state == OrderState.ACCEPTED


# ---------------------------------------------------------------------------
# History clock validation — refuse before any persistence
# ---------------------------------------------------------------------------


def test_naive_clock_refuses_first_submit(tmp_path: Path) -> None:
    def naive_now() -> datetime:
        return datetime(2026, 1, 1, 0, 0)  # no tzinfo

    broker = MockBroker(store_root=tmp_path, now_utc=naive_now)
    with pytest.raises(BrokerClockError, match="naive"):
        broker.submit(_buy("100000"))
    assert list((tmp_path / "orders").glob("*.json")) == []


def test_regressing_clock_refuses_transition_without_mutation(
    tmp_path: Path,
) -> None:
    """A record_fill whose clock is BEFORE the accepted timestamp must
    refuse fail-closed and leave the accepted order file byte-identical."""
    times = iter(
        [
            datetime(2026, 1, 1, 12, 0, tzinfo=UTC),  # submit
            datetime(2026, 1, 1, 11, 0, tzinfo=UTC),  # record_fill (regresses)
        ]
    )

    def stepping_now() -> datetime:
        return next(times)

    broker = MockBroker(store_root=tmp_path, now_utc=stepping_now)
    order = broker.submit(_sell("1.0"))
    path = tmp_path / "orders" / f"{order.client_order_id}.json"
    before = path.read_bytes()
    with pytest.raises(BrokerClockError, match="regressed"):
        broker.record_fill(
            order.client_order_id,
            Qty.from_str("0.1"),
            Money.from_str("10000"),
        )
    assert path.read_bytes() == before
    # Reload confirms the accepted order is still valid on disk.
    reloaded = MockBroker(store_root=tmp_path).get(order.client_order_id)
    assert reloaded is not None
    assert reloaded.state == OrderState.ACCEPTED


def test_non_utc_aware_clock_is_normalized_to_utc(tmp_path: Path) -> None:
    """A valid tz-aware non-UTC clock must be normalized to UTC on disk
    and survive a restart."""
    kst = timezone(timedelta(hours=9))
    submit_at = datetime(2026, 1, 1, 21, 0, tzinfo=kst)  # 12:00 UTC
    fill_at = datetime(2026, 1, 1, 22, 0, tzinfo=kst)  # 13:00 UTC
    times = iter([submit_at, fill_at])

    def kst_now() -> datetime:
        return next(times)

    broker = MockBroker(store_root=tmp_path, now_utc=kst_now)
    order = broker.submit(_sell("1.0"))
    broker.record_fill(
        order.client_order_id,
        Qty.from_str("0.1"),
        Money.from_str("10000"),
    )

    on_disk = json.loads(
        (tmp_path / "orders" / f"{order.client_order_id}.json").read_text(
            encoding="utf-8"
        )
    )
    # Both history entries persisted as UTC — the offset was normalized.
    for entry in on_disk["history"]:
        assert entry["at_utc"].endswith("+00:00")

    fresh = MockBroker(store_root=tmp_path)
    reloaded = fresh.get(order.client_order_id)
    assert reloaded is not None
    assert reloaded.history[0].at_utc == submit_at.astimezone(UTC)
    assert reloaded.history[-1].at_utc == fill_at.astimezone(UTC)
