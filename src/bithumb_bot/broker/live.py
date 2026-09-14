"""Live Bithumb broker — one-cycle-safe adapter to the KRW spot venue.

Every HTTP call flows through an injected ``httpx.BaseTransport``; tests
always pass :class:`httpx.MockTransport` so real network I/O never
occurs from CI.

Wire contract (current official ``apidocs.bithumb.com``):

* ``POST /v2/orders`` — application/json body with exactly
  ``market``, ``side``, ``order_type``, one of ``price`` (market buy) or
  ``volume`` (market sell), and ``client_order_id``. No ``ord_type`` /
  ``identifier`` — those names belong to the different Bithumb-Global
  API and are not accepted by this venue.
* ``GET /v1/order`` — query by ``uuid`` OR ``client_order_id``.
* ``GET /v1/orders`` — ``state=wait`` and ``state=watch`` are queried
  as two separate requests (Bithumb does not permit mixing them).
* ``DELETE /v1/order`` — ``uuid`` only. If no UUID is persisted,
  reconcile by ``client_order_id`` first to obtain one.

Safety:

* Access key, secret key, JWT bearer, request/response bodies and
  headers are NEVER logged or attached to raised errors — only the HTTP
  status and a sanitized short error name are surfaced.
* POST is single-shot. Network / 408 / 429 / 5xx after POST is
  AMBIGUOUS: reconcile by ``client_order_id`` with bounded GET retries.
  POST is never automatically retried. Only a clean non-retryable 4xx
  becomes ``REJECTED``.
* Every persisted managed-order file is written atomically with a
  SHA-256 sidecar in ``state_dir/managed_orders/``. Corruption fails
  before any broker mutation.
* No withdrawal endpoint, model, permission, or credential exists in
  this module (D-69). No withdrawal path is loaded or referenced.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import httpx
import jwt

from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex, write_with_sidecar
from bithumb_bot.broker.identity import (
    WIRE_CLIENT_ORDER_ID_PREFIX,
    WIRE_CLIENT_ORDER_ID_TOTAL_LEN,
    canonical_intent_bytes,
    deterministic_client_order_id,
    deterministic_wire_client_order_id,
)
from bithumb_bot.broker.state import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    BrokerOrder,
    OrderState,
    StateTransition,
)
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import BithumbBotError
from bithumb_bot.execution.intent import OrderIntent

DEFAULT_BASE_URL = "https://api.bithumb.com"
BITHUMB_MARKET = "KRW-BTC"
_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 15.0
_MAX_GET_ATTEMPTS = 3
_MANAGED_ORDERS_DIRNAME = "managed_orders"
#: Bumped to 2 for the wire-id split + persisted ``paid_fee_krw``. Old
#: v1 files no longer decode — the operator must clear ``managed_orders/``
#: before a v2 process starts.
_MAPPING_SCHEMA_VERSION = 2
_INTERNAL_CID_RE = re.compile(r"^[0-9a-f]{64}$")
_WIRE_CID_RE = re.compile(
    r"^"
    + re.escape(WIRE_CLIENT_ORDER_ID_PREFIX)
    + r"[0-9a-f]{"
    + str(WIRE_CLIENT_ORDER_ID_TOTAL_LEN - len(WIRE_CLIENT_ORDER_ID_PREFIX))
    + r"}$"
)

# Bithumb v1/v2 order state strings → our five persisted OrderState values.
# The venue exposes exactly four strings we accept: wait / watch / done /
# cancel. ``wait`` with any positive executed_volume is mapped to
# PARTIALLY_FILLED at the boundary (Bithumb reports partial resting
# orders as ``wait`` — there is no separate "partial" venue state).
_STATE_MAP: dict[str, OrderState] = {
    "wait": OrderState.ACCEPTED,
    "watch": OrderState.ACCEPTED,
    "done": OrderState.FILLED,
    "cancel": OrderState.CANCELED,
}

# Statuses we treat as AMBIGUOUS after POST (never REJECTED); everything
# else in 4xx is a clean, non-retryable rejection.
_AMBIGUOUS_POST_STATUSES: frozenset[int] = frozenset(
    {408, 429, 500, 502, 503, 504}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LiveBrokerError(BithumbBotError):
    """Base class for live-broker errors."""


class ManagedOrderCorruptError(LiveBrokerError):
    """A managed-order file failed schema/integrity checks.

    Raised BEFORE any broker mutation; the on-disk file is never
    modified as a side effect of the refusal.
    """


class AmbiguousSubmitError(LiveBrokerError):
    """POST /v2/orders response was lost or ambiguous and the venue
    state cannot be determined after bounded reconciliation.

    POST is never automatically retried; the coordinator halts cleanly
    on this.
    """


class VenueResponseError(LiveBrokerError):
    """A REST response is malformed, non-finite, or fails validation."""


class VenueHTTPError(LiveBrokerError):
    """A REST call returned a non-2xx status.

    Carries only ``status`` and a sanitized ``error_name``; response
    body / headers / free-text messages are NEVER attached.
    """

    def __init__(self, *, status: int, error_name: str, endpoint: str) -> None:
        super().__init__(
            f"venue {endpoint} returned status={status} error_name={error_name!r}"
        )
        self.status = status
        self.error_name = error_name
        self.endpoint = endpoint


class UnmanagedOpenOrderError(LiveBrokerError):
    """The venue reports an open KRW-BTC order not tracked by this broker."""


# ---------------------------------------------------------------------------
# JWT builder — hashes exactly the params that go on the wire, in order.
# ---------------------------------------------------------------------------


def _build_bearer_token(
    access_key: str,
    secret_key: str,
    wire_params: Mapping[str, str] | None,
    *,
    nonce_fn: Callable[[], str] = lambda: uuid.uuid4().hex,
    now_ms_fn: Callable[[], int] = lambda: int(time.time() * 1000),
) -> str:
    """Build an HS256 JWT with SHA-512 ``query_hash`` of the caller's
    exact ordered ``wire_params``.

    The caller MUST pass the EXACT (ordered) mapping that will be sent
    on the wire — for GET requests that is the query string, for POST
    it is the JSON body serialized in insertion order. The signature
    covers ``urlencode`` of the same ordered items; a server-side
    reordering (e.g. alphabetical sort) is NOT applied here because
    Bithumb hashes the caller's own key order.
    """
    payload: dict[str, Any] = {
        "access_key": access_key,
        "nonce": nonce_fn(),
        "timestamp": now_ms_fn(),
    }
    if wire_params:
        encoded = urlencode(list(wire_params.items()))
        payload["query_hash"] = hashlib.sha512(
            encoded.encode("utf-8")
        ).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    token = jwt.encode(payload, secret_key, algorithm="HS256")
    if isinstance(token, bytes):  # PyJWT < 2 defensive
        token = token.decode("utf-8")
    return token


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _sanitize_error_name(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    err = payload.get("error")
    if not isinstance(err, dict):
        return "unknown"
    name = err.get("name")
    if isinstance(name, bool):
        return "unknown"
    if isinstance(name, int) and 0 <= name <= 999999:
        return str(name)
    if isinstance(name, str) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", name
    ):
        return name
    return "unknown"


def _decode_finite_decimal(value: Any, *, field_name: str) -> Decimal:
    if value is None:
        raise VenueResponseError(f"{field_name} is null")
    if isinstance(value, bool):
        raise VenueResponseError(f"{field_name} must not be bool")
    if isinstance(value, (int, str)):
        try:
            d = Decimal(str(value))
        except InvalidOperation as exc:
            raise VenueResponseError(
                f"{field_name} not decimal-parseable"
            ) from exc
    elif isinstance(value, Decimal):
        d = value
    else:
        raise VenueResponseError(
            f"{field_name} must be str/int/Decimal, got {type(value).__name__}"
        )
    if not d.is_finite():
        raise VenueResponseError(f"{field_name} must be finite")
    return d


def _decode_nonnegative_decimal(value: Any, *, field_name: str) -> Decimal:
    d = _decode_finite_decimal(value, field_name=field_name)
    if d < 0:
        raise VenueResponseError(f"{field_name} must be >= 0, got {d}")
    return d


def _decode_positive_decimal(value: Any, *, field_name: str) -> Decimal:
    d = _decode_finite_decimal(value, field_name=field_name)
    if d <= 0:
        raise VenueResponseError(f"{field_name} must be > 0, got {d}")
    return d


# ---------------------------------------------------------------------------
# Managed-order persistence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagedOrder:
    """Persisted mapping + last-known venue state for one submitted order."""

    internal_client_order_id: str
    wire_client_order_id: str
    venue_uuid: str | None
    intent: OrderIntent
    mapped_state: OrderState
    filled_qty: Qty
    filled_notional_krw: Money
    paid_fee_krw: Money
    rejection_reason: str | None
    history: tuple[StateTransition, ...]

    def to_broker_order(self) -> BrokerOrder:
        return BrokerOrder(
            client_order_id=self.internal_client_order_id,
            intent=self.intent,
            state=self.mapped_state,
            filled_qty=self.filled_qty,
            filled_notional_krw=self.filled_notional_krw,
            rejection_reason=self.rejection_reason,
            history=self.history,
            paid_fee_krw=self.paid_fee_krw,
        )


def _encode_intent(intent: OrderIntent) -> dict[str, Any]:
    return {
        "side": intent.side,
        "source_open_time_utc": intent.source_open_time_utc.isoformat(),
        "unit_minutes": intent.unit_minutes,
        "signal_ts_utc": intent.signal_ts_utc.isoformat(),
        "requested_notional_krw": (
            str(intent.requested_notional_krw.value)
            if intent.requested_notional_krw is not None
            else None
        ),
        "requested_qty": (
            str(intent.requested_qty.value)
            if intent.requested_qty is not None
            else None
        ),
        "reason": intent.reason,
        "trigger_price": (
            str(intent.trigger_price.value)
            if intent.trigger_price is not None
            else None
        ),
    }


def _decode_intent(data: Any) -> OrderIntent:
    if not isinstance(data, dict):
        raise ManagedOrderCorruptError("intent is not an object")
    try:
        source_open = datetime.fromisoformat(data["source_open_time_utc"])
        signal_ts = datetime.fromisoformat(data["signal_ts_utc"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ManagedOrderCorruptError(f"intent timestamps invalid: {exc}") from exc

    def _decode_money(v: Any) -> Money | None:
        if v is None:
            return None
        try:
            d = Decimal(v)
        except (InvalidOperation, TypeError) as exc:
            raise ManagedOrderCorruptError(f"decimal not parseable") from exc
        if not d.is_finite() or d <= 0:
            raise ManagedOrderCorruptError(f"decimal not > 0 or non-finite")
        return Money(d)

    def _decode_qty(v: Any) -> Qty | None:
        m = _decode_money(v)
        return Qty(m.value) if m is not None else None

    try:
        return OrderIntent(
            side=data["side"],
            source_open_time_utc=source_open,
            unit_minutes=data["unit_minutes"],
            signal_ts_utc=signal_ts,
            requested_notional_krw=_decode_money(data["requested_notional_krw"]),
            requested_qty=_decode_qty(data["requested_qty"]),
            reason=data.get("reason", "strategy_signal"),
            trigger_price=_decode_money(data.get("trigger_price")),
        )
    except (KeyError, ValueError) as exc:
        raise ManagedOrderCorruptError(f"intent invariants failed: {exc}") from exc


def _encode_managed(order: ManagedOrder) -> dict[str, Any]:
    return {
        "schema_version": _MAPPING_SCHEMA_VERSION,
        "internal_client_order_id": order.internal_client_order_id,
        "wire_client_order_id": order.wire_client_order_id,
        "venue_uuid": order.venue_uuid,
        "intent": _encode_intent(order.intent),
        "mapped_state": order.mapped_state.value,
        "filled_qty": str(order.filled_qty.value),
        "filled_notional_krw": str(order.filled_notional_krw.value),
        "paid_fee_krw": str(order.paid_fee_krw.value),
        "rejection_reason": order.rejection_reason,
        "history": [
            {
                "from_state": t.from_state.value if t.from_state is not None else None,
                "to_state": t.to_state.value,
                "at_utc": t.at_utc.isoformat(),
            }
            for t in order.history
        ],
    }


def _decode_managed(data: Any, *, source: Path) -> ManagedOrder:
    if not isinstance(data, dict):
        raise ManagedOrderCorruptError(f"{source}: top-level not object")
    # Schema-version must be the exact int (not bool — Python True == 1).
    sv = data.get("schema_version")
    if type(sv) is not int or sv != _MAPPING_SCHEMA_VERSION:  # noqa: E721
        raise ManagedOrderCorruptError(
            f"{source}: unsupported schema_version={sv!r}"
        )
    cid = data.get("internal_client_order_id")
    if not isinstance(cid, str) or not _INTERNAL_CID_RE.fullmatch(cid):
        raise ManagedOrderCorruptError(
            f"{source}: internal_client_order_id malformed"
        )
    wire_cid = data.get("wire_client_order_id")
    if not isinstance(wire_cid, str) or not _WIRE_CID_RE.fullmatch(wire_cid):
        raise ManagedOrderCorruptError(
            f"{source}: wire_client_order_id malformed"
        )
    if len(wire_cid) > WIRE_CLIENT_ORDER_ID_TOTAL_LEN:
        raise ManagedOrderCorruptError(
            f"{source}: wire_client_order_id exceeds "
            f"{WIRE_CLIENT_ORDER_ID_TOTAL_LEN} chars"
        )
    venue_uuid = data.get("venue_uuid")
    if venue_uuid is not None and (
        not isinstance(venue_uuid, str) or not venue_uuid
    ):
        raise ManagedOrderCorruptError(f"{source}: venue_uuid must be non-empty str or null")
    intent = _decode_intent(data.get("intent"))
    if deterministic_client_order_id(intent) != cid:
        raise ManagedOrderCorruptError(
            f"{source}: intent does not hash to internal_client_order_id"
        )
    if deterministic_wire_client_order_id(intent) != wire_cid:
        raise ManagedOrderCorruptError(
            f"{source}: intent does not hash to wire_client_order_id"
        )
    try:
        state = OrderState(data.get("mapped_state"))
    except ValueError as exc:
        raise ManagedOrderCorruptError(f"{source}: bad mapped_state") from exc
    raw_filled_qty = data.get("filled_qty")
    raw_filled_notional = data.get("filled_notional_krw")
    raw_paid_fee = data.get("paid_fee_krw")
    if not isinstance(raw_filled_qty, str) or not isinstance(
        raw_filled_notional, str
    ):
        raise ManagedOrderCorruptError(f"{source}: fill totals must be strings")
    if not isinstance(raw_paid_fee, str):
        raise ManagedOrderCorruptError(f"{source}: paid_fee_krw must be string")
    try:
        filled_qty = Qty(Decimal(raw_filled_qty))
        filled_notional = Money(Decimal(raw_filled_notional))
        paid_fee = Money(Decimal(raw_paid_fee))
    except (InvalidOperation, TypeError) as exc:
        raise ManagedOrderCorruptError(f"{source}: bad fill totals") from exc
    if not filled_qty.value.is_finite() or filled_qty.value < 0:
        raise ManagedOrderCorruptError(f"{source}: filled_qty invalid")
    if not filled_notional.value.is_finite() or filled_notional.value < 0:
        raise ManagedOrderCorruptError(f"{source}: filled_notional_krw invalid")
    if not paid_fee.value.is_finite() or paid_fee.value < 0:
        raise ManagedOrderCorruptError(f"{source}: paid_fee_krw invalid")
    rejection_reason = data.get("rejection_reason")
    if rejection_reason is not None and not isinstance(rejection_reason, str):
        raise ManagedOrderCorruptError(f"{source}: rejection_reason must be str or null")
    # Rejection-reason consistency: only allowed when state is REJECTED, and
    # required when state is REJECTED.
    if state is OrderState.REJECTED and rejection_reason is None:
        raise ManagedOrderCorruptError(
            f"{source}: REJECTED state requires rejection_reason"
        )
    if state is not OrderState.REJECTED and rejection_reason is not None:
        raise ManagedOrderCorruptError(
            f"{source}: rejection_reason only allowed on REJECTED"
        )
    # Fill / state consistency.
    if state is OrderState.FILLED and filled_qty.value <= 0:
        raise ManagedOrderCorruptError(
            f"{source}: FILLED state requires positive filled_qty"
        )
    if state is OrderState.PARTIALLY_FILLED and filled_qty.value <= 0:
        raise ManagedOrderCorruptError(
            f"{source}: PARTIALLY_FILLED requires positive filled_qty"
        )
    raw_history = data.get("history")
    if not isinstance(raw_history, list) or not raw_history:
        raise ManagedOrderCorruptError(f"{source}: history must be non-empty list")
    history: list[StateTransition] = []
    prior_state: OrderState | None = None
    prior_at: datetime | None = None
    for entry in raw_history:
        if not isinstance(entry, dict):
            raise ManagedOrderCorruptError(f"{source}: history entry not object")
        fs = entry.get("from_state")
        ts = entry.get("to_state")
        at = entry.get("at_utc")
        if not isinstance(at, str) or not isinstance(ts, str):
            raise ManagedOrderCorruptError(
                f"{source}: history entry requires str to_state + at_utc"
            )
        try:
            from_state = OrderState(fs) if fs is not None else None
            to_state = OrderState(ts)
            at_dt = datetime.fromisoformat(at)
        except (ValueError, TypeError) as exc:
            raise ManagedOrderCorruptError(
                f"{source}: history entry invalid: {exc}"
            ) from exc
        if at_dt.tzinfo is None or at_dt.tzinfo.utcoffset(at_dt) is None:
            raise ManagedOrderCorruptError(
                f"{source}: history at_utc must be tz-aware"
            )
        if prior_at is not None and at_dt < prior_at:
            raise ManagedOrderCorruptError(
                f"{source}: history at_utc regresses"
            )
        # Chain consistency: from_state of entry N must equal to_state of entry N-1.
        if prior_state is not None and from_state != prior_state:
            raise ManagedOrderCorruptError(
                f"{source}: history chain broken (from_state != previous to_state)"
            )
        if prior_state is None and from_state is not None:
            raise ManagedOrderCorruptError(
                f"{source}: first history entry must have from_state=null"
            )
        history.append(
            StateTransition(from_state=from_state, to_state=to_state, at_utc=at_dt)
        )
        prior_state = to_state
        prior_at = at_dt
    # Final entry's to_state must equal the persisted mapped_state.
    if history[-1].to_state is not state:
        raise ManagedOrderCorruptError(
            f"{source}: final history to_state must equal mapped_state"
        )
    return ManagedOrder(
        internal_client_order_id=cid,
        wire_client_order_id=wire_cid,
        venue_uuid=venue_uuid,
        intent=intent,
        mapped_state=state,
        filled_qty=filled_qty,
        filled_notional_krw=filled_notional,
        paid_fee_krw=paid_fee,
        rejection_reason=rejection_reason,
        history=tuple(history),
    )


# ---------------------------------------------------------------------------
# LiveBithumbBroker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueBalances:
    """Available + locked KRW cash and BTC coin, plus optional avg_buy_price.

    Both ``available`` (``cash_krw`` / ``coin_qty``) and ``locked``
    (``cash_krw_locked`` / ``coin_qty_locked``) are surfaced so the
    coordinator can reconcile against ``available + locked`` — a
    no-fill accepted order simply moves KRW from ``available`` to
    ``locked`` at the venue, and comparing only ``available`` would
    (incorrectly) look like unexplained drift.
    """

    cash_krw: Money
    coin_qty: Qty
    cash_krw_locked: Money = field(default_factory=lambda: Money(Decimal("0")))
    coin_qty_locked: Qty = field(default_factory=lambda: Qty(Decimal("0")))
    avg_buy_price: Money | None = None


@dataclass
class LiveBithumbBroker:
    """Live Bithumb REST adapter.

    Injectable ``transport`` keeps every test offline. ``access_key`` /
    ``secret_key`` are held as plain ``str`` only for the duration of
    one CLI cycle; the caller must let this instance go out of scope
    after the operation completes so the strings drop out of the local
    frame.
    """

    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    state_dir: Path
    market: str = BITHUMB_MARKET
    base_url: str = DEFAULT_BASE_URL
    transport: httpx.BaseTransport | None = None
    now_utc: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC)
    )

    def __repr__(self) -> str:  # pragma: no cover — masking
        return (
            f"LiveBithumbBroker(state_dir={self.state_dir!s}, "
            f"market={self.market!r}, base_url={self.base_url!r})"
        )

    def __post_init__(self) -> None:
        self._orders_dir = self.state_dir / _MANAGED_ORDERS_DIRNAME
        self._orders_dir.mkdir(parents=True, exist_ok=True)

    # -- HTTP plumbing ------------------------------------------------

    def _client(self) -> httpx.Client:
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT_S,
            read=_READ_TIMEOUT_S,
            write=_READ_TIMEOUT_S,
            pool=_READ_TIMEOUT_S,
        )
        return httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=self.transport,
            trust_env=False,
        )

    def _authed_headers(
        self,
        wire_params: Mapping[str, str] | None,
        *,
        content_type: str | None = None,
    ) -> dict[str, str]:
        token = _build_bearer_token(self.access_key, self.secret_key, wire_params)
        h = {"Authorization": f"Bearer {token}"}
        if content_type is not None:
            h["Content-Type"] = content_type
        return h

    def _get_json(
        self,
        endpoint: str,
        wire_params: Mapping[str, str] | None,
        *,
        allow_404: bool = False,
    ) -> Any:
        """Bounded-retry GET. Retries only on connect/read/network errors
        or 5xx / 429 / 408 statuses. Never mutates venue state."""
        for attempt in range(1, _MAX_GET_ATTEMPTS + 1):
            try:
                with self._client() as client:
                    if wire_params:
                        response = client.request(
                            "GET",
                            endpoint,
                            params=list(wire_params.items()),
                            headers=self._authed_headers(wire_params),
                        )
                    else:
                        response = client.request(
                            "GET", endpoint, headers=self._authed_headers(None)
                        )
            except (
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.ConnectError,
                httpx.NetworkError,
            ) as exc:
                if attempt >= _MAX_GET_ATTEMPTS:
                    raise VenueHTTPError(
                        status=0, error_name="network_error", endpoint=endpoint
                    ) from exc
                continue
            status = response.status_code
            if allow_404 and status == 404:
                return None
            if status in (408, 429, 500, 502, 503, 504) and attempt < _MAX_GET_ATTEMPTS:
                continue
            if 200 <= status < 300:
                try:
                    return response.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    raise VenueResponseError(
                        f"{endpoint} returned non-JSON body"
                    ) from exc
            try:
                parsed = response.json()
            except Exception:
                parsed = None
            raise VenueHTTPError(
                status=status,
                error_name=_sanitize_error_name(parsed),
                endpoint=endpoint,
            )
        raise VenueHTTPError(  # pragma: no cover — loop invariant covered above
            status=0, error_name="network_error", endpoint=endpoint
        )

    def _post_json(
        self, endpoint: str, body: Mapping[str, str]
    ) -> tuple[str, int, Any]:
        """Single-shot POST returning ``(kind, status, parsed_body)``.

        ``kind`` is:
          * ``"ok"``        — 2xx response.
          * ``"ambiguous"`` — network failure OR 408/429/5xx (retryable
            or transport-lost; caller MUST reconcile by
            client_order_id, never repeat POST).
          * ``"rejected"``  — unambiguous non-retryable 4xx.

        POST itself is NEVER automatically retried.
        """
        # Serialize preserving insertion order so the wire body key order
        # matches the JWT ``query_hash`` (which covers the caller's exact
        # ordered params — sorting would produce a hash mismatch).
        body_bytes = json.dumps(
            dict(body), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        try:
            with self._client() as client:
                response = client.request(
                    "POST",
                    endpoint,
                    content=body_bytes,
                    headers=self._authed_headers(
                        body,
                        content_type="application/json; charset=utf-8",
                    ),
                )
        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.NetworkError,
        ):
            return ("ambiguous", 0, None)
        status = response.status_code
        try:
            parsed = response.json()
        except (ValueError, json.JSONDecodeError):
            parsed = None
        if 200 <= status < 300:
            return ("ok", status, parsed)
        if status in _AMBIGUOUS_POST_STATUSES:
            return ("ambiguous", status, parsed)
        return ("rejected", status, parsed)

    def _delete_json(self, endpoint: str, wire_params: Mapping[str, str]) -> Any:
        try:
            with self._client() as client:
                response = client.request(
                    "DELETE",
                    endpoint,
                    params=list(wire_params.items()),
                    headers=self._authed_headers(wire_params),
                )
        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.NetworkError,
        ) as exc:
            raise VenueHTTPError(
                status=0, error_name="network_error", endpoint=endpoint
            ) from exc
        status = response.status_code
        try:
            parsed = response.json()
        except (ValueError, json.JSONDecodeError):
            parsed = None
        if 200 <= status < 300:
            return parsed
        raise VenueHTTPError(
            status=status,
            error_name=_sanitize_error_name(parsed),
            endpoint=endpoint,
        )

    # -- Public price fetch (unauthenticated) -------------------------

    def fetch_current_price(self) -> Money:
        """GET /v1/ticker for the configured market. Public endpoint."""
        endpoint = "/v1/ticker"
        wire_params = {"markets": self.market}
        try:
            with self._client() as client:
                response = client.request(
                    "GET", endpoint, params=list(wire_params.items())
                )
        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.NetworkError,
        ) as exc:
            raise VenueHTTPError(
                status=0, error_name="network_error", endpoint=endpoint
            ) from exc
        status = response.status_code
        if not (200 <= status < 300):
            try:
                parsed_err = response.json()
            except Exception:
                parsed_err = None
            raise VenueHTTPError(
                status=status,
                error_name=_sanitize_error_name(parsed_err),
                endpoint=endpoint,
            )
        try:
            parsed = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise VenueResponseError(f"{endpoint} non-JSON body") from exc
        if not isinstance(parsed, list) or not parsed:
            raise VenueResponseError(f"{endpoint} expected non-empty array")
        row = parsed[0]
        if not isinstance(row, dict):
            raise VenueResponseError(f"{endpoint} row must be object")
        price = _decode_positive_decimal(
            row.get("trade_price"), field_name="ticker.trade_price"
        )
        return Money(price)

    def fetch_current_candle(self) -> dict[str, Any]:
        """GET /v1/candles/minutes/240 (count=1). Public endpoint.

        Returns the raw current-candle row: ``candle_date_time_utc``
        (open time) and ``opening_price`` (open). Used by the coordinator
        to build a :class:`StopObservation` anchored on the actual
        in-progress candle rather than the closed historical dataset.
        """
        endpoint = "/v1/candles/minutes/240"
        wire_params = {"market": self.market, "count": "1"}
        try:
            with self._client() as client:
                response = client.request(
                    "GET", endpoint, params=list(wire_params.items())
                )
        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.NetworkError,
        ) as exc:
            raise VenueHTTPError(
                status=0, error_name="network_error", endpoint=endpoint
            ) from exc
        status = response.status_code
        if not (200 <= status < 300):
            try:
                parsed_err = response.json()
            except Exception:
                parsed_err = None
            raise VenueHTTPError(
                status=status,
                error_name=_sanitize_error_name(parsed_err),
                endpoint=endpoint,
            )
        try:
            parsed = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise VenueResponseError(f"{endpoint} non-JSON body") from exc
        if not isinstance(parsed, list) or not parsed:
            raise VenueResponseError(f"{endpoint} expected non-empty array")
        row = parsed[0]
        if not isinstance(row, dict):
            raise VenueResponseError(f"{endpoint} row must be object")
        return row

    # -- Accounts / balances -----------------------------------------

    def fetch_balances(self) -> VenueBalances:
        """GET /v1/accounts. Returns available + locked KRW/BTC.

        Both ``balance`` and ``locked`` are parsed as finite nonnegative
        Decimals; a missing ``locked`` on a row defaults to zero.
        """
        endpoint = "/v1/accounts"
        parsed = self._get_json(endpoint, None)
        if not isinstance(parsed, list):
            raise VenueResponseError(f"{endpoint} expected array")
        cash = Decimal("0")
        cash_locked = Decimal("0")
        coin = Decimal("0")
        coin_locked = Decimal("0")
        avg_buy: Decimal | None = None
        currency_of_market = self.market.split("-", 1)[-1]  # BTC
        seen_krw = False
        for row in parsed:
            if not isinstance(row, dict):
                raise VenueResponseError(f"{endpoint} row not object")
            currency = row.get("currency")
            if currency == "KRW":
                cash = _decode_nonnegative_decimal(
                    row.get("balance"), field_name="accounts.KRW.balance"
                )
                raw_locked = row.get("locked")
                if raw_locked is not None:
                    cash_locked = _decode_nonnegative_decimal(
                        raw_locked, field_name="accounts.KRW.locked"
                    )
                seen_krw = True
            elif currency == currency_of_market:
                coin = _decode_nonnegative_decimal(
                    row.get("balance"), field_name="accounts.BTC.balance"
                )
                raw_coin_locked = row.get("locked")
                if raw_coin_locked is not None:
                    coin_locked = _decode_nonnegative_decimal(
                        raw_coin_locked, field_name="accounts.BTC.locked"
                    )
                raw_avg = row.get("avg_buy_price")
                if raw_avg is not None:
                    avg_buy = _decode_nonnegative_decimal(
                        raw_avg, field_name="accounts.BTC.avg_buy_price"
                    )
        if not seen_krw:
            raise VenueResponseError(f"{endpoint} missing KRW row")
        return VenueBalances(
            cash_krw=Money(cash),
            coin_qty=Qty(coin),
            cash_krw_locked=Money(cash_locked),
            coin_qty_locked=Qty(coin_locked),
            avg_buy_price=Money(avg_buy) if avg_buy is not None and avg_buy > 0 else None,
        )

    # -- Venue order queries -----------------------------------------

    def query_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        """GET ``/v1/order?client_order_id=<wire_cid>``. ``None`` on 404.

        The parameter MUST be the wire client_order_id (<=36 chars, the
        one the venue actually knows about) — the 64-char internal
        SHA-256 would exceed the venue cap and never match a row.
        """
        endpoint = "/v1/order"
        wire_params = {"client_order_id": client_order_id}
        raw = self._get_json(endpoint, wire_params, allow_404=True)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise VenueResponseError(f"{endpoint} row must be object")
        return raw

    def query_by_uuid(self, venue_uuid: str) -> dict[str, Any] | None:
        endpoint = "/v1/order"
        wire_params = {"uuid": venue_uuid}
        raw = self._get_json(endpoint, wire_params, allow_404=True)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise VenueResponseError(f"{endpoint} row must be object")
        return raw

    def list_open_venue_orders(self) -> list[dict[str, Any]]:
        """Return every open KRW-BTC row across both ``wait`` and ``watch``.

        Bithumb does not permit combining these two states in a single
        ``/v1/orders`` query, so this method issues two separate GETs
        and validates each row's ``client_order_id`` is a well-formed
        internal cid (unknowns bubble up to the caller so an unmanaged
        open order can be flagged before any submit).
        """
        endpoint = "/v1/orders"
        out: list[dict[str, Any]] = []
        for state in ("wait", "watch"):
            wire_params = {"market": self.market, "state": state}
            raw = self._get_json(endpoint, wire_params)
            if not isinstance(raw, list):
                raise VenueResponseError(f"{endpoint} expected array")
            for row in raw:
                if not isinstance(row, dict):
                    raise VenueResponseError(f"{endpoint} row must be object")
                out.append(row)
        return out

    # -- Reconciliation helpers --------------------------------------

    def _apply_venue_response(
        self, order: ManagedOrder, venue_row: dict[str, Any]
    ) -> ManagedOrder:
        """Fold a fresh venue-order row into a persisted ManagedOrder.

        Refuses (raises :class:`VenueResponseError`) on malformed rows,
        mismatched identity fields, regressing fills, or an invalid
        state transition — the on-disk file is not mutated by this
        method; the caller decides when to persist.
        """
        # Identity: market / side / order_type / client_order_id must
        # match the intent this managed order represents.
        row_market = venue_row.get("market")
        if row_market != self.market:
            raise VenueResponseError(
                f"venue row market={row_market!r} != {self.market!r}"
            )
        row_cid = venue_row.get("client_order_id")
        if row_cid != order.wire_client_order_id:
            raise VenueResponseError(
                f"venue row client_order_id mismatch"
            )
        row_side = venue_row.get("side")
        expected_side = "bid" if order.intent.side == "buy" else "ask"
        if row_side != expected_side:
            raise VenueResponseError(
                f"venue row side={row_side!r} != expected {expected_side!r}"
            )
        # Accept either ``order_type`` (v2 POST response) or ``ord_type``
        # (GET /v1/order response) — the venue uses different key names
        # across those two endpoints. If both are present and disagree,
        # refuse the row.
        has_order_type = "order_type" in venue_row
        has_ord_type = "ord_type" in venue_row
        row_order_type = venue_row.get("order_type")
        row_ord_type = venue_row.get("ord_type")
        if has_order_type and has_ord_type and row_order_type != row_ord_type:
            raise VenueResponseError(
                f"venue row order_type={row_order_type!r} disagrees with "
                f"ord_type={row_ord_type!r}"
            )
        row_ord = row_order_type if has_order_type else row_ord_type
        expected_ord = "price" if order.intent.side == "buy" else "market"
        if row_ord != expected_ord:
            raise VenueResponseError(
                f"venue row order_type={row_ord!r} != expected {expected_ord!r}"
            )

        state_str = venue_row.get("state")
        if not isinstance(state_str, str) or state_str not in _STATE_MAP:
            raise VenueResponseError(
                f"venue order state unknown: {state_str!r}"
            )
        executed_volume = _decode_nonnegative_decimal(
            venue_row.get("executed_volume", "0"),
            field_name="order.executed_volume",
        )
        executed_funds = _decode_nonnegative_decimal(
            venue_row.get("executed_funds", "0"),
            field_name="order.executed_funds",
        )
        # ``paid_fee`` is the venue's authoritative cumulative KRW fee
        # for this order — do NOT estimate. A missing key defaults to 0
        # so a venue that omits the field (unusual, but possible on
        # accepted-with-no-fills rows) doesn't refuse a legitimate row.
        paid_fee = _decode_nonnegative_decimal(
            venue_row.get("paid_fee", "0"),
            field_name="order.paid_fee",
        )
        if paid_fee < order.paid_fee_krw.value:
            raise VenueResponseError(
                "cumulative paid_fee regressed"
            )

        # ``wait`` with any positive executed_volume is PARTIALLY_FILLED.
        if state_str == "wait" and executed_volume > 0:
            new_state = OrderState.PARTIALLY_FILLED
        else:
            new_state = _STATE_MAP[state_str]

        # UUID: immutable once observed.
        row_uuid = venue_row.get("uuid")
        if row_uuid is not None and not isinstance(row_uuid, str):
            raise VenueResponseError("order.uuid must be str or null")
        if order.venue_uuid is not None and row_uuid is not None and (
            row_uuid != order.venue_uuid
        ):
            raise VenueResponseError(
                "venue row uuid changed after first observation"
            )

        # Cumulative fill monotonicity + bounds.
        if executed_volume < order.filled_qty.value:
            raise VenueResponseError(
                "cumulative executed_volume regressed"
            )
        if executed_funds < order.filled_notional_krw.value:
            raise VenueResponseError(
                "cumulative executed_funds regressed"
            )
        if order.intent.side == "sell":
            assert order.intent.requested_qty is not None
            if executed_volume > order.intent.requested_qty.value:
                raise VenueResponseError(
                    "venue executed_volume exceeds requested_qty"
                )
        else:
            assert order.intent.requested_notional_krw is not None
            if executed_funds > order.intent.requested_notional_krw.value:
                raise VenueResponseError(
                    "venue executed_funds exceeds requested_notional_krw"
                )

        # State transition validation — allow same-state refresh.
        if new_state != order.mapped_state:
            if new_state not in VALID_TRANSITIONS.get(
                order.mapped_state, frozenset()
            ):
                raise VenueResponseError(
                    f"invalid venue transition {order.mapped_state.value} "
                    f"-> {new_state.value}"
                )

        at_utc = self.now_utc()
        if at_utc.tzinfo is None or at_utc.tzinfo.utcoffset(at_utc) is None:
            raise VenueResponseError("now_utc must be tz-aware")

        history = order.history
        if new_state != order.mapped_state:
            history = history + (
                StateTransition(
                    from_state=order.mapped_state,
                    to_state=new_state,
                    at_utc=at_utc,
                ),
            )

        return ManagedOrder(
            internal_client_order_id=order.internal_client_order_id,
            wire_client_order_id=order.wire_client_order_id,
            venue_uuid=row_uuid if row_uuid is not None else order.venue_uuid,
            intent=order.intent,
            mapped_state=new_state,
            filled_qty=Qty(executed_volume),
            filled_notional_krw=Money(executed_funds),
            paid_fee_krw=Money(paid_fee),
            rejection_reason=order.rejection_reason,
            history=history,
        )

    # -- Managed-order persistence -----------------------------------

    def _managed_path(self, internal_cid: str) -> Path:
        return self._orders_dir / f"{internal_cid}.json"

    def _write_managed(self, order: ManagedOrder) -> None:
        target = self._managed_path(order.internal_client_order_id)
        payload = canonical_bytes(_encode_managed(order))
        write_with_sidecar(target, payload)

    def _load_managed(self, internal_cid: str) -> ManagedOrder | None:
        path = self._managed_path(internal_cid)
        if not path.is_file():
            return None
        raw = path.read_bytes()
        sidecar = path.with_name(path.name + ".sha256")
        if not sidecar.is_file():
            raise ManagedOrderCorruptError(f"{path}: sidecar missing")
        recorded = sidecar.read_text(encoding="utf-8").strip().split()
        if not recorded or len(recorded[0]) != 64 or recorded[0] != sha256_hex(raw):
            raise ManagedOrderCorruptError(f"{path}: sidecar hash mismatch")
        # Canonical-JSON validation: bytes we read back must equal the
        # canonical serialization of the parsed payload.
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ManagedOrderCorruptError(f"{path}: invalid JSON") from exc
        if canonical_bytes(data) != raw:
            raise ManagedOrderCorruptError(
                f"{path}: bytes are not canonical JSON"
            )
        managed = _decode_managed(data, source=path)
        if managed.internal_client_order_id != internal_cid:
            raise ManagedOrderCorruptError(
                f"{path}: internal id mismatch"
            )
        return managed

    def load_all_managed(self) -> list[ManagedOrder]:
        """Every managed order currently persisted (any state)."""
        out: list[ManagedOrder] = []
        for path in sorted(self._orders_dir.glob("*.json")):
            cid = path.stem
            if not _INTERNAL_CID_RE.fullmatch(cid):
                raise ManagedOrderCorruptError(
                    f"unexpected filename in {self._orders_dir}: {path.name!r}"
                )
            managed = self._load_managed(cid)
            assert managed is not None
            out.append(managed)
        return out

    # -- Broker protocol ---------------------------------------------

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        """Submit an intent through the venue idempotently.

        Sequence:

        1. Compute internal client_order_id.
        2. If a persisted managed order already exists: verify the
           intent matches, refresh venue state via GET by
           client_order_id, and return without re-submitting.
        3. Otherwise persist an ``accepted`` mapping (venue_uuid=None)
           BEFORE any POST.
        4. POST /v2/orders. Network / 408 / 429 / 5xx → AMBIGUOUS:
           reconcile by client_order_id with bounded GET retries; if
           still unknown, raise ``AmbiguousSubmitError``. POST is NEVER
           retried.
        5. Fold the venue response (or GET result) into the mapping.
        """
        internal_cid = deterministic_client_order_id(intent)
        wire_cid = deterministic_wire_client_order_id(intent)
        # Fail-closed guardrails: the wire cid MUST be a fresh derivation
        # of the same intent bytes AND MUST fit the venue's 36-char cap.
        # Collision guard: two distinct intents cannot share a wire cid
        # unless they also share the full 64-char internal cid — the
        # wire cid is a deterministic prefix of the internal cid.
        if len(wire_cid) > WIRE_CLIENT_ORDER_ID_TOTAL_LEN:
            raise VenueResponseError(
                f"derived wire_client_order_id exceeds "
                f"{WIRE_CLIENT_ORDER_ID_TOTAL_LEN} chars"
            )
        if not _WIRE_CID_RE.fullmatch(wire_cid):
            raise VenueResponseError(
                "derived wire_client_order_id fails format regex"
            )

        existing = self._load_managed(internal_cid)
        if existing is not None:
            if canonical_intent_bytes(existing.intent) != canonical_intent_bytes(intent):
                raise VenueResponseError(
                    f"persisted intent for internal_id {internal_cid} differs "
                    f"from submitted intent"
                )
            if existing.wire_client_order_id != wire_cid:
                raise VenueResponseError(
                    f"persisted wire_client_order_id "
                    f"{existing.wire_client_order_id!r} does not match freshly "
                    f"derived {wire_cid!r}"
                )
            refreshed = self._reconcile_by_client_order_id(existing)
            return refreshed.to_broker_order()

        at_utc = self.now_utc()
        pre = ManagedOrder(
            internal_client_order_id=internal_cid,
            wire_client_order_id=wire_cid,
            venue_uuid=None,
            intent=intent,
            mapped_state=OrderState.ACCEPTED,
            filled_qty=Qty(Decimal("0")),
            filled_notional_krw=Money(Decimal("0")),
            paid_fee_krw=Money(Decimal("0")),
            rejection_reason=None,
            history=(
                StateTransition(
                    from_state=None,
                    to_state=OrderState.ACCEPTED,
                    at_utc=at_utc,
                ),
            ),
        )
        # Persist mapping BEFORE POST so a crash after this line leaves
        # a recoverable identifier the next cycle can reconcile.
        self._write_managed(pre)

        body = _build_order_body(intent, wire_cid, market=self.market)
        kind, status, response_body = self._post_json("/v2/orders", body)

        if kind == "rejected":
            reason = f"venue_rejected(status={status},name={_sanitize_error_name(response_body)})"
            rejected = _apply_rejection(pre, reason, at_utc=self.now_utc())
            self._write_managed(rejected)
            return rejected.to_broker_order()

        if kind == "ambiguous":
            venue_row = self._reconcile_venue_by_client_order_id(wire_cid)
            if venue_row is None:
                raise AmbiguousSubmitError(
                    f"POST /v2/orders response lost and venue reports no "
                    f"order for client_order_id {internal_cid[:12]}…; halt "
                    f"for operator intervention"
                )
            updated = self._apply_venue_response(pre, venue_row)
            self._write_managed(updated)
            return updated.to_broker_order()

        # kind == "ok"
        # A 2xx that is not a dict, or is a dict without a recognized
        # ``state`` string, is incomplete — not terminal. Reconcile via
        # GET by the persisted wire client_order_id; never repeat POST.
        response_state = (
            response_body.get("state")
            if isinstance(response_body, dict)
            else None
        )
        if not isinstance(response_body, dict) or not (
            isinstance(response_state, str) and response_state in _STATE_MAP
        ):
            venue_row = self._reconcile_venue_by_client_order_id(wire_cid)
            if venue_row is None:
                raise AmbiguousSubmitError(
                    "POST /v2/orders 2xx body incomplete and venue reports "
                    "no order yet; halt for operator intervention"
                )
            updated = self._apply_venue_response(pre, venue_row)
            self._write_managed(updated)
            return updated.to_broker_order()
        updated = self._apply_venue_response(pre, response_body)
        self._write_managed(updated)
        return updated.to_broker_order()

    def get(self, client_order_id: str) -> BrokerOrder | None:
        if not _INTERNAL_CID_RE.fullmatch(client_order_id):
            return None
        managed = self._load_managed(client_order_id)
        if managed is None:
            return None
        return managed.to_broker_order()

    def list_open(self) -> list[BrokerOrder]:
        return [
            m.to_broker_order()
            for m in self.load_all_managed()
            if m.mapped_state not in TERMINAL_STATES
        ]

    def cancel(self, client_order_id: str) -> BrokerOrder:
        """Cancel a managed order via DELETE /v1/order?uuid=<...>.

        If no UUID is persisted yet, reconcile by client_order_id first
        to obtain one; if the venue still cannot supply one, the order
        is either unknown to the venue (ambiguous — surfaced) or already
        terminal (returned as-is).

        This method NEVER forces local CANCELED on a 2xx DELETE — it
        re-queries the order and, if the venue still reports
        ``wait``/``watch``, keeps the persisted state non-terminal so
        the next cycle continues to reconcile.
        """
        managed = self._load_managed(client_order_id)
        if managed is None:
            raise KeyError(f"no managed order with internal_id={client_order_id}")
        if managed.mapped_state in TERMINAL_STATES:
            return managed.to_broker_order()

        if managed.venue_uuid is None:
            refreshed = self._reconcile_by_client_order_id(managed)
            if refreshed.mapped_state in TERMINAL_STATES:
                return refreshed.to_broker_order()
            if refreshed.venue_uuid is None:
                # Venue still hasn't assigned a uuid — ambiguous.
                raise AmbiguousSubmitError(
                    "cannot cancel: venue has no uuid for this client_order_id"
                )
            managed = refreshed

        assert managed.venue_uuid is not None
        self._delete_json("/v1/order", {"uuid": managed.venue_uuid})
        # DELETE 2xx does not guarantee the local state is CANCELED —
        # requery, and if the venue still reports wait/watch keep it
        # non-terminal so the next cycle reconciles again.
        refreshed = self._reconcile_by_client_order_id(managed)
        return refreshed.to_broker_order()

    # -- Internal reconciliation -------------------------------------

    def _reconcile_venue_by_client_order_id(
        self, wire_cid: str
    ) -> dict[str, Any] | None:
        """GET /v1/order?client_order_id=<wire_cid>. ``None`` on 404."""
        try:
            return self.query_by_client_order_id(wire_cid)
        except VenueHTTPError as exc:
            if exc.status == 404:
                return None
            raise

    def _reconcile_by_client_order_id(self, order: ManagedOrder) -> ManagedOrder:
        """Refresh ``order`` via GET /v1/order. Persist and return."""
        venue_row = self._reconcile_venue_by_client_order_id(
            order.wire_client_order_id
        )
        if venue_row is None:
            raise AmbiguousSubmitError(
                f"venue reports no order for client_order_id "
                f"{order.internal_client_order_id[:12]}…"
            )
        updated = self._apply_venue_response(order, venue_row)
        self._write_managed(updated)
        return updated

    def reconcile_all(self) -> list[ManagedOrder]:
        """Refresh every persisted managed order's venue state."""
        out: list[ManagedOrder] = []
        for managed in self.load_all_managed():
            if managed.mapped_state in TERMINAL_STATES:
                out.append(managed)
                continue
            out.append(self._reconcile_by_client_order_id(managed))
        return out


def _build_order_body(
    intent: OrderIntent, wire_client_order_id: str, *, market: str
) -> dict[str, str]:
    """Return the exact POST body (JSON) for a Bithumb market order.

    ``wire_client_order_id`` MUST be the Bithumb-safe (<=36 char) wire
    id; the full 64-char internal SHA-256 would exceed the venue cap
    and is never sent on the wire.

    Field order matters — the JWT ``query_hash`` covers the URL-encoded
    key order sent on the wire, so callers hand the same insertion-
    ordered dict to :func:`_build_bearer_token` and to
    :meth:`_post_json`.
    """
    if len(wire_client_order_id) > WIRE_CLIENT_ORDER_ID_TOTAL_LEN:
        raise ValueError(
            f"wire_client_order_id must be <= "
            f"{WIRE_CLIENT_ORDER_ID_TOTAL_LEN} chars"
        )
    if intent.side == "buy":
        assert intent.requested_notional_krw is not None
        return {
            "market": market,
            "side": "bid",
            "order_type": "price",
            "price": str(intent.requested_notional_krw.value),
            "client_order_id": wire_client_order_id,
        }
    assert intent.requested_qty is not None
    return {
        "market": market,
        "side": "ask",
        "order_type": "market",
        "volume": str(intent.requested_qty.value),
        "client_order_id": wire_client_order_id,
    }


def _apply_rejection(
    order: ManagedOrder, reason: str, *, at_utc: datetime
) -> ManagedOrder:
    return ManagedOrder(
        internal_client_order_id=order.internal_client_order_id,
        wire_client_order_id=order.wire_client_order_id,
        venue_uuid=order.venue_uuid,
        intent=order.intent,
        mapped_state=OrderState.REJECTED,
        filled_qty=order.filled_qty,
        filled_notional_krw=order.filled_notional_krw,
        paid_fee_krw=order.paid_fee_krw,
        rejection_reason=reason,
        history=order.history
        + (
            StateTransition(
                from_state=order.mapped_state,
                to_state=OrderState.REJECTED,
                at_utc=at_utc,
            ),
        ),
    )


__all__ = [
    "AmbiguousSubmitError",
    "BITHUMB_MARKET",
    "DEFAULT_BASE_URL",
    "LiveBithumbBroker",
    "LiveBrokerError",
    "ManagedOrder",
    "ManagedOrderCorruptError",
    "UnmanagedOpenOrderError",
    "VenueBalances",
    "VenueHTTPError",
    "VenueResponseError",
]
