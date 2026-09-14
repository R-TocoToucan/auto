"""Focused offline tests for LiveBithumbBroker (Bithumb v1/v2 REST).

Every HTTP request flows through ``httpx.MockTransport``; no real network
I/O ever occurs. Sensitive credential material never appears in recorded
transport requests, error messages, or persisted files.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
import jwt
import pytest

from bithumb_bot.broker.identity import (
    WIRE_CLIENT_ORDER_ID_TOTAL_LEN,
    deterministic_client_order_id,
    deterministic_wire_client_order_id,
)
from bithumb_bot.broker.live import (
    AmbiguousSubmitError,
    LiveBithumbBroker,
    ManagedOrderCorruptError,
    VenueHTTPError,
    VenueResponseError,
    _build_bearer_token,
    _build_order_body,
)
from bithumb_bot.broker.state import OrderState
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent

BASE_OPEN = datetime(2026, 3, 15, 0, 0, tzinfo=UTC)
SECRET = "test-secret-do-not-log-please-32-plus-bytes-long-for-hmac"
ACCESS = "test-access-key-32-bytes-or-more"


def _buy_intent(notional: str = "1000000") -> OrderIntent:
    return OrderIntent(
        side="buy",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=240,
        signal_ts_utc=BASE_OPEN + timedelta(minutes=240),
        requested_notional_krw=Money.from_str(notional),
        requested_qty=None,
    )


def _sell_intent(qty: str = "0.005") -> OrderIntent:
    return OrderIntent(
        side="sell",
        source_open_time_utc=BASE_OPEN,
        unit_minutes=240,
        signal_ts_utc=BASE_OPEN + timedelta(minutes=240),
        requested_notional_krw=None,
        requested_qty=Qty.from_str(qty),
    )


def _wait_response(
    wire_cid: str,
    *,
    uuid_str: str = "u-1",
    side: str = "bid",
    order_type: str = "price",
    executed_volume: str = "0",
    executed_funds: str = "0",
    paid_fee: str = "0",
) -> dict[str, Any]:
    return {
        "uuid": uuid_str,
        "market": "KRW-BTC",
        "side": side,
        "order_type": order_type,
        "state": "wait",
        "client_order_id": wire_cid,
        "executed_volume": executed_volume,
        "executed_funds": executed_funds,
        "paid_fee": paid_fee,
    }


def _done_response(
    wire_cid: str,
    *,
    uuid_str: str = "u-1",
    side: str = "bid",
    order_type: str = "price",
    executed_volume: str = "0.005",
    executed_funds: str = "1000000",
    paid_fee: str = "0",
) -> dict[str, Any]:
    r = _wait_response(
        wire_cid,
        uuid_str=uuid_str,
        side=side,
        order_type=order_type,
        executed_volume=executed_volume,
        executed_funds=executed_funds,
        paid_fee=paid_fee,
    )
    r["state"] = "done"
    return r


def _make_broker(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    now: datetime | None = None,
) -> LiveBithumbBroker:
    transport = httpx.MockTransport(handler)
    return LiveBithumbBroker(
        access_key=ACCESS,
        secret_key=SECRET,
        state_dir=tmp_path,
        transport=transport,
        now_utc=(lambda: now if now is not None else datetime.now(UTC)),
    )


# ---------------------------------------------------------------------------
# D1: Wire contract — POST /v2/orders JSON body shape
# ---------------------------------------------------------------------------


def test_buy_body_uses_official_field_names_and_no_forbidden_keys() -> None:
    intent = _buy_intent("500000")
    wire_cid = deterministic_wire_client_order_id(intent)
    body = _build_order_body(intent, wire_cid, market="KRW-BTC")
    assert body == {
        "market": "KRW-BTC",
        "side": "bid",
        "order_type": "price",
        "price": "500000",
        "client_order_id": wire_cid,
    }
    # Forbidden legacy keys must not appear.
    assert "ord_type" not in body
    assert "identifier" not in body


def test_sell_body_uses_official_field_names_and_volume() -> None:
    intent = _sell_intent("0.001")
    wire_cid = deterministic_wire_client_order_id(intent)
    body = _build_order_body(intent, wire_cid, market="KRW-BTC")
    assert body == {
        "market": "KRW-BTC",
        "side": "ask",
        "order_type": "market",
        "volume": "0.001",
        "client_order_id": wire_cid,
    }


def test_decimal_serialization_is_string_never_float() -> None:
    intent = _buy_intent("1000000.00")
    wire_cid = deterministic_wire_client_order_id(intent)
    body = _build_order_body(intent, wire_cid, market="KRW-BTC")
    assert body["price"] == "1000000.00"


def test_post_sends_json_body_with_correct_content_type(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/orders":
            captured["content_type"] = request.headers.get("Content-Type")
            captured["body_bytes"] = request.content
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": {"name": "not_found"}})

    broker = _make_broker(tmp_path, handler)
    broker.submit(intent)
    # JSON body — parseable as JSON, contains the exact fields.
    body = json.loads(captured["body_bytes"].decode("utf-8"))
    assert body["market"] == "KRW-BTC"
    assert body["side"] == "bid"
    assert body["order_type"] == "price"
    assert body["client_order_id"] == wire_cid
    # Wire cid must fit the 36-char Bithumb cap.
    assert len(wire_cid) <= WIRE_CLIENT_ORDER_ID_TOTAL_LEN
    assert body["price"] == "1000000"
    assert "ord_type" not in body
    assert "identifier" not in body
    # Content-Type is application/json; charset=utf-8.
    assert captured["content_type"] == "application/json; charset=utf-8"


# ---------------------------------------------------------------------------
# D1: JWT query_hash matches the EXACT ordered params sent on the wire
# ---------------------------------------------------------------------------


def test_jwt_query_hash_uses_caller_key_order_not_alphabetical() -> None:
    # Non-alphabetical intentional order.
    body = {"market": "KRW-BTC", "side": "bid", "order_type": "price", "price": "1000"}
    token = _build_bearer_token(
        ACCESS, SECRET, body,
        nonce_fn=lambda: "n-fixed", now_ms_fn=lambda: 123,
    )
    decoded = jwt.decode(token, SECRET, algorithms=["HS256"])
    expected = hashlib.sha512(
        urlencode(list(body.items())).encode("utf-8")
    ).hexdigest()
    # Sanity check: this differs from an alphabetical-sort hash.
    alt = hashlib.sha512(
        urlencode(sorted(body.items())).encode("utf-8")
    ).hexdigest()
    assert expected != alt
    assert decoded["query_hash"] == expected
    assert decoded["query_hash_alg"] == "SHA512"


def test_jwt_matches_actual_post_body_encoding(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/orders":
            captured["auth"] = request.headers.get("Authorization", "")
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": {"name": "not_found"}})

    broker = _make_broker(tmp_path, handler)
    broker.submit(intent)
    token = captured["auth"].removeprefix("Bearer ")
    decoded = jwt.decode(token, SECRET, algorithms=["HS256"])
    # The body params in insertion order == what was hashed.
    expected = hashlib.sha512(
        urlencode(list(captured["body"].items())).encode("utf-8")
    ).hexdigest()
    assert decoded["query_hash"] == expected


def test_jwt_no_query_hash_when_no_params() -> None:
    token = _build_bearer_token(
        ACCESS, SECRET, None,
        nonce_fn=lambda: "n", now_ms_fn=lambda: 1,
    )
    decoded = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert "query_hash" not in decoded


# ---------------------------------------------------------------------------
# D1: GET uses client_order_id; DELETE uses uuid only
# ---------------------------------------------------------------------------


def test_query_by_client_order_id_uses_correct_query_param(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)
    seen_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/order":
            seen_params.update(dict(request.url.params))
            return httpx.Response(200, json=_wait_response(wire_cid))
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    broker.query_by_client_order_id(wire_cid)
    assert seen_params == {"client_order_id": wire_cid}
    assert "identifier" not in seen_params


def test_delete_uses_uuid_only_never_client_order_id_substitute(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)
    delete_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_wait_response(wire_cid, uuid_str="u-A"))
        if request.method == "DELETE" and request.url.path == "/v1/order":
            delete_params.update(dict(request.url.params))
            return httpx.Response(200, json={
                "uuid": "u-A", "market": "KRW-BTC", "side": "bid",
                "order_type": "price", "client_order_id": wire_cid,
                "state": "cancel", "executed_volume": "0", "executed_funds": "0",
                "paid_fee": "0",
            })
        if request.method == "GET" and request.url.path == "/v1/order":
            r = _wait_response(wire_cid, uuid_str="u-A")
            r["state"] = "cancel"
            return httpx.Response(200, json=r)
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    order = broker.submit(intent)
    broker.cancel(order.client_order_id)
    assert delete_params == {"uuid": "u-A"}
    assert "identifier" not in delete_params
    assert "client_order_id" not in delete_params


# ---------------------------------------------------------------------------
# D1: list_open_venue_orders queries BOTH wait and watch separately
# ---------------------------------------------------------------------------


def test_list_open_queries_wait_and_watch_as_separate_requests(tmp_path: Path) -> None:
    seen_states: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/orders":
            state = request.url.params.get("state")
            seen_states.append(state)
            if state == "wait":
                return httpx.Response(200, json=[
                    {"client_order_id": "c-1", "state": "wait", "market": "KRW-BTC"},
                ])
            if state == "watch":
                return httpx.Response(200, json=[
                    {"client_order_id": "c-2", "state": "watch", "market": "KRW-BTC"},
                ])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    rows = broker.list_open_venue_orders()
    assert set(seen_states) == {"wait", "watch"}
    assert len(rows) == 2
    assert {r["client_order_id"] for r in rows} == {"c-1", "c-2"}


# ---------------------------------------------------------------------------
# D2: State mapping: wait+positive_fill maps PARTIALLY_FILLED
# ---------------------------------------------------------------------------


def test_wait_with_positive_fill_maps_partially_filled(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_wait_response(
                wire_cid,
                executed_volume="0.002",  # positive fill while wait
                executed_funds="400000",
            ))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/v1/order":
            return httpx.Response(200, json=_wait_response(
                wire_cid, executed_volume="0.002", executed_funds="400000",
            ))
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    order = broker.submit(intent)
    assert order.state == OrderState.PARTIALLY_FILLED
    assert order.filled_qty.value == Decimal("0.002")


def test_state_map_has_no_trade_state() -> None:
    from bithumb_bot.broker.live import _STATE_MAP

    assert "trade" not in _STATE_MAP
    assert _STATE_MAP["wait"] == OrderState.ACCEPTED
    assert _STATE_MAP["watch"] == OrderState.ACCEPTED
    assert _STATE_MAP["done"] == OrderState.FILLED
    assert _STATE_MAP["cancel"] == OrderState.CANCELED


# ---------------------------------------------------------------------------
# D2: response validation — mismatches / regressions refuse without mutation
# ---------------------------------------------------------------------------


def test_cumulative_fill_decrease_refuses_without_mutation(tmp_path: Path) -> None:
    intent = _buy_intent()
    internal_cid = deterministic_client_order_id(intent)
    wire_cid = deterministic_wire_client_order_id(intent)
    state = {"phase": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state["phase"] = 1
            return httpx.Response(200, json=_wait_response(
                wire_cid, executed_volume="0.003", executed_funds="600000",
            ))
        if request.method == "GET" and request.url.path == "/v1/order":
            # Later refresh reports fewer fills — must refuse.
            return httpx.Response(200, json=_wait_response(
                wire_cid, executed_volume="0.001", executed_funds="200000",
            ))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    order = broker.submit(intent)
    assert order.state == OrderState.PARTIALLY_FILLED
    # Snapshot mid-state so we can detect if refuse mutated the file.
    file_bytes_before = (
        tmp_path / "managed_orders" / f"{internal_cid}.json"
    ).read_bytes()
    with pytest.raises(VenueResponseError):
        broker.reconcile_all()
    file_bytes_after = (
        tmp_path / "managed_orders" / f"{internal_cid}.json"
    ).read_bytes()
    assert file_bytes_before == file_bytes_after


def test_venue_row_mismatched_market_refuses(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            row = _wait_response(wire_cid)
            row["market"] = "KRW-ETH"  # wrong market
            return httpx.Response(200, json=row)
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    with pytest.raises(VenueResponseError):
        broker.submit(intent)


def test_uuid_changing_after_first_observation_refuses(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)
    state = {"phase": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state["phase"] = 1
            return httpx.Response(200, json=_wait_response(wire_cid, uuid_str="u-A"))
        if request.method == "GET" and request.url.path == "/v1/order":
            # UUID changed — must refuse.
            return httpx.Response(200, json=_wait_response(wire_cid, uuid_str="u-B"))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    broker.submit(intent)
    with pytest.raises(VenueResponseError):
        broker.reconcile_all()


# ---------------------------------------------------------------------------
# D2: POST ambiguity — network / 408 / 429 / 5xx never become REJECTED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", [
    "network",
    408,
    429,
    500,
    503,
])
def test_post_ambiguity_never_becomes_rejected_and_never_reposts(
    tmp_path: Path, failure: Any
) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            if failure == "network":
                raise httpx.ConnectError("boom")
            return httpx.Response(failure, json={"error": {"name": "srv_error"}})
        if request.method == "GET" and request.url.path == "/v1/order":
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    order = broker.submit(intent)
    # POST is NOT re-issued — the recovery is a GET.
    assert posts == 1
    # Ambiguity resolved via GET → order is ACCEPTED, not REJECTED.
    assert order.state == OrderState.ACCEPTED


def test_lost_post_and_venue_still_unknown_halts_with_ambiguous_error(
    tmp_path: Path,
) -> None:
    intent = _buy_intent()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ConnectError("boom")
        if request.method == "GET" and request.url.path == "/v1/order":
            return httpx.Response(404, json={"error": {"name": "not_found"}})
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    with pytest.raises(AmbiguousSubmitError):
        broker.submit(intent)


def test_clean_4xx_becomes_rejected(tmp_path: Path) -> None:
    intent = _buy_intent()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(400, json={"error": {"name": "bad_params"}})
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    order = broker.submit(intent)
    assert order.state == OrderState.REJECTED


# ---------------------------------------------------------------------------
# D2: cancel never forces CANCELED; DELETE 2xx that leaves wait/watch stays
#     non-terminal
# ---------------------------------------------------------------------------


def test_cancel_delete_2xx_but_still_wait_keeps_non_terminal(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_wait_response(wire_cid, uuid_str="u-A"))
        if request.method == "DELETE" and request.url.path == "/v1/order":
            # Venue accepted the cancel request but has not yet applied.
            return httpx.Response(200, json={"accepted": True})
        if request.method == "GET" and request.url.path == "/v1/order":
            # Still wait after the DELETE.
            return httpx.Response(200, json=_wait_response(wire_cid, uuid_str="u-A"))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    order = broker.submit(intent)
    result = broker.cancel(order.client_order_id)
    # The venue still reports wait; we DO NOT force CANCELED locally.
    assert result.state == OrderState.ACCEPTED


def test_cancel_after_venue_actually_canceled_returns_canceled(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_wait_response(wire_cid, uuid_str="u-B"))
        if request.method == "DELETE":
            return httpx.Response(200, json={"accepted": True})
        if request.method == "GET" and request.url.path == "/v1/order":
            row = _wait_response(wire_cid, uuid_str="u-B")
            row["state"] = "cancel"
            return httpx.Response(200, json=row)
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    order = broker.submit(intent)
    result = broker.cancel(order.client_order_id)
    assert result.state == OrderState.CANCELED


# ---------------------------------------------------------------------------
# D2: persisted-state / sidecar / canonical-JSON corruption fails without
#     mutation
# ---------------------------------------------------------------------------


def test_tampered_persisted_state_fails_before_mutation(tmp_path: Path) -> None:
    intent = _buy_intent()
    internal_cid = deterministic_client_order_id(intent)
    wire_cid = deterministic_wire_client_order_id(intent)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/v1/order":
            return httpx.Response(200, json=_wait_response(wire_cid))
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    broker.submit(intent)
    # Corrupt the file — sidecar hash no longer matches.
    path = tmp_path / "managed_orders" / f"{internal_cid}.json"
    original = path.read_bytes()
    path.write_bytes(original + b" ")  # trailing whitespace breaks canonical form
    with pytest.raises(ManagedOrderCorruptError):
        broker.get(internal_cid)


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def test_fetch_balances_parses_krw_and_coin(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/accounts":
            return httpx.Response(
                200,
                json=[
                    {"currency": "KRW", "balance": "5000000", "locked": "0"},
                    {"currency": "BTC", "balance": "0.01",
                     "avg_buy_price": "100000000"},
                ],
            )
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    bal = broker.fetch_balances()
    assert bal.cash_krw.value == Decimal("5000000")
    assert bal.coin_qty.value == Decimal("0.01")
    # No locked reported → defaults to zero.
    assert bal.cash_krw_locked.value == Decimal("0")
    assert bal.coin_qty_locked.value == Decimal("0")
    assert bal.avg_buy_price is not None
    assert bal.avg_buy_price.value == Decimal("100000000")


def test_fetch_balances_parses_locked_from_both_rows(tmp_path: Path) -> None:
    """Every KRW row and every BTC row's ``locked`` must round-trip.

    Bithumb moves funds from ``balance`` (available) to ``locked`` for
    an open resting order. The coordinator compares against
    ``available + locked`` so a no-fill accepted buy MUST NOT look
    like unexplained drift.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/accounts":
            return httpx.Response(
                200,
                json=[
                    {"currency": "KRW", "balance": "1000000",
                     "locked": "4000000"},
                    {"currency": "BTC", "balance": "0.005",
                     "locked": "0.001",
                     "avg_buy_price": "100000000"},
                ],
            )
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    bal = broker.fetch_balances()
    assert bal.cash_krw.value == Decimal("1000000")
    assert bal.cash_krw_locked.value == Decimal("4000000")
    assert bal.coin_qty.value == Decimal("0.005")
    assert bal.coin_qty_locked.value == Decimal("0.001")


def test_fetch_balances_refuses_negative_locked(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"currency": "KRW", "balance": "1000", "locked": "-1"},
                {"currency": "BTC", "balance": "0"},
            ],
        )

    broker = _make_broker(tmp_path, handler)
    with pytest.raises(Exception) as exc_info:
        broker.fetch_balances()
    assert "must be >= 0" in str(exc_info.value)


def test_fetch_balances_refuses_negative(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"currency": "KRW", "balance": "-1"},
                {"currency": "BTC", "balance": "0"},
            ],
        )

    broker = _make_broker(tmp_path, handler)
    with pytest.raises(Exception) as exc_info:
        broker.fetch_balances()
    assert "must be >= 0" in str(exc_info.value)


def test_fetch_balances_refuses_missing_krw_row(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"currency": "BTC", "balance": "0"}])

    broker = _make_broker(tmp_path, handler)
    with pytest.raises(Exception) as exc_info:
        broker.fetch_balances()
    assert "missing KRW" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Public current-candle fetch for the protective-stop observation
# ---------------------------------------------------------------------------


def test_fetch_current_candle_returns_row(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/candles/minutes/240":
            return httpx.Response(200, json=[{
                "candle_date_time_utc": "2026-03-15T00:00:00",
                "opening_price": "100000000",
            }])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    row = broker.fetch_current_candle()
    assert row["candle_date_time_utc"] == "2026-03-15T00:00:00"
    assert row["opening_price"] == "100000000"


# ---------------------------------------------------------------------------
# Secrets are never leaked
# ---------------------------------------------------------------------------


def test_secret_never_appears_in_repr(tmp_path: Path) -> None:
    broker = _make_broker(tmp_path, lambda r: httpx.Response(200, json={}))
    assert SECRET not in repr(broker)
    assert ACCESS not in repr(broker)


def test_secret_never_appears_in_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"name": "server_error"}})

    broker = _make_broker(tmp_path, handler)
    with pytest.raises(VenueHTTPError) as exc_info:
        broker.fetch_balances()
    assert SECRET not in str(exc_info.value)
    assert ACCESS not in str(exc_info.value)


def test_secret_never_persisted_to_managed_order_file(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/v1/order":
            return httpx.Response(200, json=_wait_response(wire_cid))
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    broker.submit(intent)
    for path in (tmp_path / "managed_orders").glob("*"):
        blob = path.read_bytes()
        assert SECRET.encode() not in blob
        assert ACCESS.encode() not in blob


# ---------------------------------------------------------------------------
# Idempotent duplicate submit — POST is issued exactly once for same intent
# ---------------------------------------------------------------------------


def test_duplicate_submit_produces_single_venue_post(tmp_path: Path) -> None:
    intent = _buy_intent()
    wire_cid = deterministic_wire_client_order_id(intent)
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/order":
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    first = broker.submit(intent)
    second = broker.submit(intent)
    assert first.client_order_id == second.client_order_id
    assert posts == 1


# ---------------------------------------------------------------------------
# Wire-cid split + paid-fee regressions
# ---------------------------------------------------------------------------


def test_wire_cid_is_le_36_chars_and_stable_across_restart(tmp_path: Path) -> None:
    """Wire cid must fit the venue cap and survive a process restart.

    The persisted managed-order file records both the 64-char internal
    id (SHA-256 collision guard) AND the 36-char wire id. Loading the
    file on a fresh broker instance must yield the same wire id — a
    reconciliation POST/GET has to reproduce the same wire cid the
    venue already knows about.
    """
    intent = _buy_intent()
    internal_cid = deterministic_client_order_id(intent)
    wire_cid = deterministic_wire_client_order_id(intent)
    assert len(wire_cid) <= WIRE_CLIENT_ORDER_ID_TOTAL_LEN
    assert wire_cid.startswith("bt-")
    assert internal_cid != wire_cid

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/order":
            return httpx.Response(200, json=_wait_response(wire_cid))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    broker.submit(intent)

    # Restart: new broker instance reading the same on-disk file must
    # recover the wire id byte-identically.
    broker2 = _make_broker(tmp_path, handler)
    persisted = broker2._load_managed(internal_cid)
    assert persisted is not None
    assert persisted.internal_client_order_id == internal_cid
    assert persisted.wire_client_order_id == wire_cid


def test_venue_row_wire_cid_mismatch_refuses(tmp_path: Path) -> None:
    """A venue row whose ``client_order_id`` disagrees with the persisted
    wire id must refuse — never trust the wrong order back."""
    intent = _buy_intent()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            row = _wait_response("bt-DOES-NOT-MATCH-EXPECTED-WIRE-CID")
            return httpx.Response(200, json=row)
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    with pytest.raises(VenueResponseError):
        broker.submit(intent)


def test_paid_fee_is_persisted_and_regression_refuses(tmp_path: Path) -> None:
    """Cumulative ``paid_fee`` must monotonically grow just like fills.

    Also: the standard 0.25% fee on a normal buy round-trips through
    the persisted mapping so a subsequent cycle can compute an honest
    KRW delta.
    """
    intent = _buy_intent("1000000")
    internal_cid = deterministic_client_order_id(intent)
    wire_cid = deterministic_wire_client_order_id(intent)
    phase = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_wait_response(
                wire_cid,
                executed_volume="0.005",
                executed_funds="500000",
                paid_fee="1250",  # 0.25% of 500000
            ))
        if request.method == "GET" and request.url.path == "/v1/order":
            phase["n"] += 1
            if phase["n"] == 1:
                # Regression on paid_fee — must refuse.
                return httpx.Response(200, json=_wait_response(
                    wire_cid,
                    executed_volume="0.005",
                    executed_funds="500000",
                    paid_fee="500",
                ))
            return httpx.Response(200, json=_wait_response(
                wire_cid,
                executed_volume="0.005",
                executed_funds="500000",
                paid_fee="1250",
            ))
        if request.method == "GET" and request.url.path == "/v1/orders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    broker = _make_broker(tmp_path, handler)
    order = broker.submit(intent)
    persisted = broker._load_managed(internal_cid)
    assert persisted is not None
    assert persisted.paid_fee_krw.value == Decimal("1250")

    file_before = (tmp_path / "managed_orders" / f"{internal_cid}.json").read_bytes()
    with pytest.raises(VenueResponseError):
        broker.reconcile_all()
    file_after = (tmp_path / "managed_orders" / f"{internal_cid}.json").read_bytes()
    assert file_before == file_after


def test_paid_fee_v1_schema_file_is_refused(tmp_path: Path) -> None:
    """An old schema_version=1 mapping file must be refused cleanly."""
    import json as _json

    from bithumb_bot.artifact.canonical import canonical_bytes, write_with_sidecar

    intent = _buy_intent()
    internal_cid = deterministic_client_order_id(intent)
    orders_dir = tmp_path / "managed_orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    # Handcraft a v1-shaped payload (missing wire + paid_fee fields).
    v1 = {
        "schema_version": 1,
        "internal_client_order_id": internal_cid,
        "venue_uuid": None,
        "intent": {
            "side": "buy",
            "source_open_time_utc": intent.source_open_time_utc.isoformat(),
            "unit_minutes": 240,
            "signal_ts_utc": intent.signal_ts_utc.isoformat(),
            "requested_notional_krw": "1000000",
            "requested_qty": None,
            "reason": "strategy_signal",
            "trigger_price": None,
        },
        "mapped_state": "accepted",
        "filled_qty": "0",
        "filled_notional_krw": "0",
        "rejection_reason": None,
        "history": [
            {
                "from_state": None,
                "to_state": "accepted",
                "at_utc": "2026-03-15T00:00:00+00:00",
            }
        ],
    }
    write_with_sidecar(orders_dir / f"{internal_cid}.json", canonical_bytes(v1))

    broker = _make_broker(tmp_path, lambda r: httpx.Response(404))
    with pytest.raises(ManagedOrderCorruptError):
        broker._load_managed(internal_cid)
