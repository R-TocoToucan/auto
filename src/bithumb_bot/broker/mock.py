"""Idempotent persistent MockBroker for the future live-order path.

Design rules (spec):

1. Every accepted intent is persisted to disk BEFORE any simulated fill.
   A crash between ``submit`` and the first fill leaves an intact
   ``accepted`` order that the same intent (same ``client_order_id``)
   will re-attach to.
2. ``client_order_id`` is a deterministic SHA-256 of the *canonical*
   ``OrderIntent`` — timestamps normalized to UTC, ``Decimal`` values
   normalized so that ``Decimal("100000")`` and ``Decimal("100000.00")``
   hash identically. Naive timestamps and non-finite Decimals are
   refused fail-closed.
3. Persisted ``Decimal`` values cross disk as strings, never through
   ``float``; on read they must parse to finite Decimals in the
   permitted sign range (requested > 0, filled >= 0).
4. Corrupt or malformed persisted state — invalid JSON, missing keys,
   unknown state strings, hash mismatch, out-of-range decimals, broken
   history chain, non-monotonic history timestamps, or unexpected files
   in ``orders/`` — is refused with ``BrokerStateCorrupt`` and never
   causes any file to be modified.
5. Invalid state transitions and fills that would exceed the intent's
   requested quantity/notional are refused *before* any write, with
   ``InvalidStateTransition`` / ``FillExceedsIntent`` respectively.
6. No HTTP, WebSocket, Bithumb API, credential, or environment reads
   — this module operates entirely on the local filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bithumb_bot.broker.state import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    BrokerOrder,
    OrderState,
    StateTransition,
)
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent

SCHEMA_VERSION = 1
_CLIENT_ORDER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class BrokerError(Exception):
    """Base for MockBroker errors."""


class BrokerStateCorrupt(BrokerError):
    """Persisted state failed schema/integrity checks. No files modified."""


class InvalidStateTransition(BrokerError):
    """A requested state transition is not permitted by the state DAG."""


class FillExceedsIntent(BrokerError):
    """A record_fill would push cumulative fill above the intent's request."""


def _default_now() -> datetime:
    return datetime.now(UTC)


def _canonical_datetime(dt: datetime) -> str:
    """UTC-normalized ISO-8601 string. Naive datetimes are refused."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"naive datetime not permitted: {dt.isoformat()}")
    return dt.astimezone(UTC).isoformat()


def _canonical_decimal(value: Decimal) -> str:
    """Canonical string form derived directly from ``Decimal.as_tuple()``.

    Equal numeric values produce equal strings — ``Decimal('100000')`` and
    ``Decimal('100000.00')`` both fold to ``+1E5``. Distinct values of
    arbitrary precision are NEVER rounded together: this function reads
    the raw coefficient/exponent triple, so no context-dependent rounding
    (as ``.normalize()`` would apply) can collapse two high-precision
    values that differ beyond the active context's precision.

    Non-finite Decimals are refused.
    """
    if not value.is_finite():
        raise ValueError(f"non-finite Decimal not permitted: {value}")
    tup = value.as_tuple()
    digits: tuple[int, ...] = tup.digits
    exponent = tup.exponent
    if not isinstance(exponent, int):
        # is_finite() already excludes n/N/F exponents; this is a hard
        # invariant guard, not user-reachable.
        raise ValueError(f"non-finite Decimal exponent: {exponent!r}")
    if all(d == 0 for d in digits):
        # Fold every representation of zero (including -0, 0.00, 0E+3)
        # to a single canonical form.
        return "+0E0"
    end = len(digits)
    while end > 1 and digits[end - 1] == 0:
        end -= 1
        exponent += 1
    coeff = "".join(str(d) for d in digits[:end])
    sign_char = "-" if tup.sign == 1 else "+"
    return f"{sign_char}{coeff}E{exponent}"


def _canonical_intent_bytes(intent: OrderIntent) -> bytes:
    """Deterministic byte-for-byte serialization used ONLY for hashing.

    Two intents with equal fields — even if their datetimes carry
    different UTC offsets or their Decimals carry different trailing-zero
    representations — always produce equal bytes here.
    """
    payload = {
        "side": intent.side,
        "source_open_time_utc": _canonical_datetime(intent.source_open_time_utc),
        "unit_minutes": intent.unit_minutes,
        "signal_ts_utc": _canonical_datetime(intent.signal_ts_utc),
        "requested_notional_krw": (
            _canonical_decimal(intent.requested_notional_krw.value)
            if intent.requested_notional_krw is not None
            else None
        ),
        "requested_qty": (
            _canonical_decimal(intent.requested_qty.value)
            if intent.requested_qty is not None
            else None
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def deterministic_client_order_id(intent: OrderIntent) -> str:
    """SHA-256 hex (64 chars) of the canonical intent bytes.

    Raises ``ValueError`` if the intent carries a naive datetime or a
    non-finite Decimal — those inputs cannot be canonicalized safely.
    """
    return hashlib.sha256(_canonical_intent_bytes(intent)).hexdigest()


def _parse_iso_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise BrokerStateCorrupt(f"timestamp missing tzinfo: {s!r}")
    return dt


class MockBroker:
    """Persistent, idempotent, in-process order-lifecycle simulator.

    ``submit`` persists (never mutates) an ``accepted`` order file. A
    subsequent ``submit`` of the same intent — including after process
    restart — reads that file and returns the existing order, so no
    duplicate is ever created. ``record_fill`` / ``cancel`` / ``reject``
    each perform one atomic write following a validated transition and
    a validated fill-bound check.
    """

    def __init__(
        self,
        store_root: Path,
        now_utc: Callable[[], datetime] | None = None,
    ) -> None:
        self._orders_dir = store_root / "orders"
        self._orders_dir.mkdir(parents=True, exist_ok=True)
        self._now: Callable[[], datetime] = (
            now_utc if now_utc is not None else _default_now
        )

    # -- Broker protocol methods -------------------------------------

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        cid = deterministic_client_order_id(intent)
        existing = self._load(cid)
        if existing is not None:
            if _canonical_intent_bytes(existing.intent) != _canonical_intent_bytes(intent):
                raise BrokerStateCorrupt(
                    f"client_order_id {cid} collision: persisted intent differs "
                    f"from submitted intent"
                )
            return existing
        order = BrokerOrder(
            client_order_id=cid,
            intent=intent,
            state=OrderState.ACCEPTED,
            filled_qty=Qty.from_str("0"),
            filled_notional_krw=Money.from_str("0"),
            rejection_reason=None,
            history=(
                StateTransition(
                    from_state=None,
                    to_state=OrderState.ACCEPTED,
                    at_utc=self._now(),
                ),
            ),
        )
        self._write(order)
        return order

    def get(self, client_order_id: str) -> BrokerOrder | None:
        if not _CLIENT_ORDER_ID_RE.fullmatch(client_order_id):
            return None
        return self._load(client_order_id)

    def list_open(self) -> list[BrokerOrder]:
        open_orders: list[BrokerOrder] = []
        for path in sorted(self._orders_dir.glob("*.json")):
            cid = path.stem
            if not _CLIENT_ORDER_ID_RE.fullmatch(cid):
                raise BrokerStateCorrupt(
                    f"unexpected filename in orders directory: {path.name!r}"
                )
            order = self._load(cid)
            if order is None:
                raise BrokerStateCorrupt(
                    f"{path} disappeared between glob and read"
                )
            if order.state not in TERMINAL_STATES:
                open_orders.append(order)
        return open_orders

    def cancel(self, client_order_id: str) -> BrokerOrder:
        return self._transition_no_fill(
            client_order_id,
            target=OrderState.CANCELED,
            rejection_reason=None,
        )

    # -- Simulator-only mutations ------------------------------------

    def record_fill(
        self,
        client_order_id: str,
        fill_qty: Qty,
        fill_notional_krw: Money,
        *,
        complete: bool = False,
    ) -> BrokerOrder:
        """Apply one simulated fill, transitioning to partial or filled.

        ``complete=True`` transitions to ``filled`` (terminal); otherwise
        the target is ``partially_filled``. Fill amounts must be > 0.
        Refused BEFORE any write if the cumulative fill would exceed
        the intent's request: buy side is bounded by
        ``requested_notional_krw``; sell side by ``requested_qty``.
        """
        if fill_qty.value <= 0:
            raise ValueError("fill_qty must be > 0")
        if fill_notional_krw.value <= 0:
            raise ValueError("fill_notional_krw must be > 0")
        order = self._require(client_order_id)
        target = OrderState.FILLED if complete else OrderState.PARTIALLY_FILLED
        self._require_transition(order.state, target)

        new_qty_value = order.filled_qty.value + fill_qty.value
        new_notional_value = order.filled_notional_krw.value + fill_notional_krw.value
        _require_within_intent(order.intent, new_qty_value, new_notional_value)

        new = BrokerOrder(
            client_order_id=order.client_order_id,
            intent=order.intent,
            state=target,
            filled_qty=Qty(new_qty_value),
            filled_notional_krw=Money(new_notional_value),
            rejection_reason=order.rejection_reason,
            history=order.history
            + (
                StateTransition(
                    from_state=order.state,
                    to_state=target,
                    at_utc=self._now(),
                ),
            ),
        )
        self._write(new)
        return new

    def reject(self, client_order_id: str, reason: str) -> BrokerOrder:
        """Move an accepted order to rejected (venue-refused)."""
        if not reason:
            raise ValueError("reason must be a non-empty string")
        return self._transition_no_fill(
            client_order_id,
            target=OrderState.REJECTED,
            rejection_reason=reason,
        )

    # -- Internals ---------------------------------------------------

    def _transition_no_fill(
        self,
        client_order_id: str,
        *,
        target: OrderState,
        rejection_reason: str | None,
    ) -> BrokerOrder:
        order = self._require(client_order_id)
        self._require_transition(order.state, target)
        new = BrokerOrder(
            client_order_id=order.client_order_id,
            intent=order.intent,
            state=target,
            filled_qty=order.filled_qty,
            filled_notional_krw=order.filled_notional_krw,
            rejection_reason=(
                rejection_reason
                if rejection_reason is not None
                else order.rejection_reason
            ),
            history=order.history
            + (
                StateTransition(
                    from_state=order.state,
                    to_state=target,
                    at_utc=self._now(),
                ),
            ),
        )
        self._write(new)
        return new

    def _require(self, client_order_id: str) -> BrokerOrder:
        if not _CLIENT_ORDER_ID_RE.fullmatch(client_order_id):
            raise KeyError(f"malformed client_order_id: {client_order_id!r}")
        order = self._load(client_order_id)
        if order is None:
            raise KeyError(f"no order with client_order_id={client_order_id}")
        return order

    @staticmethod
    def _require_transition(current: OrderState, target: OrderState) -> None:
        if target not in VALID_TRANSITIONS[current]:
            raise InvalidStateTransition(
                f"invalid transition {current.value} -> {target.value}"
            )

    def _path(self, client_order_id: str) -> Path:
        return self._orders_dir / f"{client_order_id}.json"

    def _load(self, client_order_id: str) -> BrokerOrder | None:
        path = self._path(client_order_id)
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BrokerStateCorrupt(f"failed to read {path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrokerStateCorrupt(f"invalid JSON in {path}: {exc}") from exc
        return _decode_order(data, expected_cid=client_order_id, source=path)

    def _write(self, order: BrokerOrder) -> None:
        payload = _encode_order(order)
        target = self._path(order.client_order_id)
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{order.client_order_id}.",
            suffix=".tmp",
            dir=self._orders_dir,
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _require_within_intent(
    intent: OrderIntent, new_qty: Decimal, new_notional: Decimal
) -> None:
    """Refuse a cumulative fill that would exceed the intent's request."""
    if intent.side == "sell":
        assert intent.requested_qty is not None  # OrderIntent invariant
        if new_qty > intent.requested_qty.value:
            raise FillExceedsIntent(
                f"cumulative fill_qty {new_qty} exceeds requested_qty "
                f"{intent.requested_qty.value}"
            )
    else:  # buy
        assert intent.requested_notional_krw is not None  # OrderIntent invariant
        if new_notional > intent.requested_notional_krw.value:
            raise FillExceedsIntent(
                f"cumulative filled_notional_krw {new_notional} exceeds "
                f"requested_notional_krw {intent.requested_notional_krw.value}"
            )


def _encode_order(order: BrokerOrder) -> dict[str, Any]:
    intent = order.intent
    return {
        "schema_version": SCHEMA_VERSION,
        "client_order_id": order.client_order_id,
        "intent": {
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
        },
        "state": order.state.value,
        "filled_qty": str(order.filled_qty.value),
        "filled_notional_krw": str(order.filled_notional_krw.value),
        "rejection_reason": order.rejection_reason,
        "history": [
            {
                "from_state": (
                    t.from_state.value if t.from_state is not None else None
                ),
                "to_state": t.to_state.value,
                "at_utc": t.at_utc.isoformat(),
            }
            for t in order.history
        ],
    }


def _decode_decimal(value: Any, *, field: str, source: Path) -> Decimal:
    if not isinstance(value, str):
        raise BrokerStateCorrupt(
            f"{source}: {field} must be a JSON string, got {type(value).__name__}"
        )
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise BrokerStateCorrupt(
            f"{source}: {field} is not a valid decimal: {value!r}"
        ) from exc
    if not d.is_finite():
        raise BrokerStateCorrupt(
            f"{source}: {field} must be finite, got {value!r}"
        )
    return d


def _decode_order(data: Any, *, expected_cid: str, source: Path) -> BrokerOrder:
    if not isinstance(data, dict):
        raise BrokerStateCorrupt(f"{source}: top-level payload not a JSON object")
    try:
        schema_version = data["schema_version"]
        cid = data["client_order_id"]
        raw_intent = data["intent"]
        state_str = data["state"]
        filled_qty_str = data["filled_qty"]
        filled_notional_str = data["filled_notional_krw"]
        rejection_reason = data["rejection_reason"]
        raw_history = data["history"]
    except KeyError as exc:
        raise BrokerStateCorrupt(f"{source}: missing key {exc}") from exc

    if schema_version != SCHEMA_VERSION:
        raise BrokerStateCorrupt(
            f"{source}: unsupported schema_version={schema_version!r}"
        )
    if not isinstance(cid, str) or cid != expected_cid:
        raise BrokerStateCorrupt(
            f"{source}: client_order_id mismatch (file={expected_cid} payload={cid!r})"
        )
    try:
        state = OrderState(state_str)
    except ValueError as exc:
        raise BrokerStateCorrupt(f"{source}: unknown state {state_str!r}") from exc
    if rejection_reason is not None and not isinstance(rejection_reason, str):
        raise BrokerStateCorrupt(f"{source}: rejection_reason must be string or null")
    if not isinstance(raw_intent, dict):
        raise BrokerStateCorrupt(f"{source}: intent must be object")
    if not isinstance(raw_history, list):
        raise BrokerStateCorrupt(f"{source}: history must be list")

    intent = _decode_intent(raw_intent, source=source)

    try:
        computed_cid = deterministic_client_order_id(intent)
    except ValueError as exc:
        raise BrokerStateCorrupt(
            f"{source}: persisted intent not canonicalizable: {exc}"
        ) from exc
    if computed_cid != expected_cid:
        raise BrokerStateCorrupt(
            f"{source}: persisted intent does not hash to client_order_id "
            f"{expected_cid} (got {computed_cid})"
        )

    filled_qty_value = _decode_decimal(
        filled_qty_str, field="filled_qty", source=source
    )
    if filled_qty_value < 0:
        raise BrokerStateCorrupt(
            f"{source}: filled_qty must be nonnegative, got {filled_qty_value}"
        )
    filled_notional_value = _decode_decimal(
        filled_notional_str, field="filled_notional_krw", source=source
    )
    if filled_notional_value < 0:
        raise BrokerStateCorrupt(
            f"{source}: filled_notional_krw must be nonnegative, got "
            f"{filled_notional_value}"
        )

    history_tuple = tuple(
        _decode_history_entry(h, source=source) for h in raw_history
    )
    _validate_history(history_tuple, final_state=state, source=source)
    _validate_state_consistency(
        state=state,
        intent=intent,
        filled_qty=filled_qty_value,
        filled_notional=filled_notional_value,
        rejection_reason=rejection_reason,
        source=source,
    )

    return BrokerOrder(
        client_order_id=cid,
        intent=intent,
        state=state,
        filled_qty=Qty(filled_qty_value),
        filled_notional_krw=Money(filled_notional_value),
        rejection_reason=rejection_reason,
        history=history_tuple,
    )


_ZERO_FILL_STATES: frozenset[OrderState] = frozenset(
    {OrderState.ACCEPTED, OrderState.REJECTED}
)
_POSITIVE_FILL_STATES: frozenset[OrderState] = frozenset(
    {OrderState.PARTIALLY_FILLED, OrderState.FILLED}
)


def _validate_state_consistency(
    *,
    state: OrderState,
    intent: OrderIntent,
    filled_qty: Decimal,
    filled_notional: Decimal,
    rejection_reason: str | None,
    source: Path,
) -> None:
    """Load-time enforcement of the invariants ``record_fill`` /
    ``cancel`` / ``reject`` uphold at write time."""
    if state == OrderState.REJECTED:
        if not rejection_reason:
            raise BrokerStateCorrupt(
                f"{source}: state {state.value} requires a non-empty "
                f"rejection_reason"
            )
    else:
        if rejection_reason is not None:
            raise BrokerStateCorrupt(
                f"{source}: rejection_reason must be null for state "
                f"{state.value}, got {rejection_reason!r}"
            )

    if state in _ZERO_FILL_STATES:
        if filled_qty != 0 or filled_notional != 0:
            raise BrokerStateCorrupt(
                f"{source}: state {state.value} requires zero fill totals, "
                f"got filled_qty={filled_qty}, "
                f"filled_notional_krw={filled_notional}"
            )
    elif state in _POSITIVE_FILL_STATES:
        if filled_qty <= 0 or filled_notional <= 0:
            raise BrokerStateCorrupt(
                f"{source}: state {state.value} requires positive fill "
                f"totals, got filled_qty={filled_qty}, "
                f"filled_notional_krw={filled_notional}"
            )
    # canceled: fills may be zero (canceled from accepted) or positive
    # (canceled from partially_filled) — no state-level constraint.

    if intent.side == "sell":
        assert intent.requested_qty is not None  # OrderIntent invariant
        if filled_qty > intent.requested_qty.value:
            raise BrokerStateCorrupt(
                f"{source}: persisted filled_qty {filled_qty} exceeds "
                f"intent.requested_qty {intent.requested_qty.value}"
            )
    else:  # buy
        assert intent.requested_notional_krw is not None  # OrderIntent invariant
        if filled_notional > intent.requested_notional_krw.value:
            raise BrokerStateCorrupt(
                f"{source}: persisted filled_notional_krw {filled_notional} "
                f"exceeds intent.requested_notional_krw "
                f"{intent.requested_notional_krw.value}"
            )


def _decode_intent(data: dict[str, Any], *, source: Path) -> OrderIntent:
    try:
        side = data["side"]
        source_open_str = data["source_open_time_utc"]
        unit_minutes = data["unit_minutes"]
        signal_ts_str = data["signal_ts_utc"]
        requested_notional_str = data["requested_notional_krw"]
        requested_qty_str = data["requested_qty"]
    except KeyError as exc:
        raise BrokerStateCorrupt(f"{source}: intent missing key {exc}") from exc
    if side not in ("buy", "sell"):
        raise BrokerStateCorrupt(f"{source}: intent.side invalid: {side!r}")
    if isinstance(unit_minutes, bool) or not isinstance(unit_minutes, int):
        raise BrokerStateCorrupt(f"{source}: intent.unit_minutes must be int")
    if not isinstance(source_open_str, str) or not isinstance(signal_ts_str, str):
        raise BrokerStateCorrupt(f"{source}: intent timestamps must be strings")
    try:
        source_open = _parse_iso_utc(source_open_str)
        signal_ts = _parse_iso_utc(signal_ts_str)
    except BrokerStateCorrupt:
        raise
    except ValueError as exc:
        raise BrokerStateCorrupt(f"{source}: intent timestamp invalid: {exc}") from exc

    if requested_notional_str is None:
        requested_notional: Money | None = None
    else:
        notional_value = _decode_decimal(
            requested_notional_str,
            field="intent.requested_notional_krw",
            source=source,
        )
        if notional_value <= 0:
            raise BrokerStateCorrupt(
                f"{source}: intent.requested_notional_krw must be > 0, got "
                f"{notional_value}"
            )
        requested_notional = Money(notional_value)

    if requested_qty_str is None:
        requested_qty: Qty | None = None
    else:
        qty_value = _decode_decimal(
            requested_qty_str, field="intent.requested_qty", source=source
        )
        if qty_value <= 0:
            raise BrokerStateCorrupt(
                f"{source}: intent.requested_qty must be > 0, got {qty_value}"
            )
        requested_qty = Qty(qty_value)

    try:
        return OrderIntent(
            side=side,
            source_open_time_utc=source_open,
            unit_minutes=unit_minutes,
            signal_ts_utc=signal_ts,
            requested_notional_krw=requested_notional,
            requested_qty=requested_qty,
        )
    except ValueError as exc:
        raise BrokerStateCorrupt(f"{source}: intent failed invariants: {exc}") from exc


def _decode_history_entry(data: Any, *, source: Path) -> StateTransition:
    if not isinstance(data, dict):
        raise BrokerStateCorrupt(f"{source}: history entry must be object")
    try:
        from_state_str = data["from_state"]
        to_state_str = data["to_state"]
        at_utc_str = data["at_utc"]
    except KeyError as exc:
        raise BrokerStateCorrupt(
            f"{source}: history entry missing key {exc}"
        ) from exc
    if not isinstance(at_utc_str, str):
        raise BrokerStateCorrupt(f"{source}: history at_utc must be string")
    try:
        from_state = (
            OrderState(from_state_str) if from_state_str is not None else None
        )
        to_state = OrderState(to_state_str)
    except ValueError as exc:
        raise BrokerStateCorrupt(
            f"{source}: history entry state invalid: {exc}"
        ) from exc
    try:
        at_utc = _parse_iso_utc(at_utc_str)
    except BrokerStateCorrupt:
        raise
    except ValueError as exc:
        raise BrokerStateCorrupt(
            f"{source}: history at_utc parse error: {exc}"
        ) from exc
    return StateTransition(from_state=from_state, to_state=to_state, at_utc=at_utc)


def _validate_history(
    history: tuple[StateTransition, ...],
    *,
    final_state: OrderState,
    source: Path,
) -> None:
    if not history:
        raise BrokerStateCorrupt(f"{source}: history is empty")
    first = history[0]
    if first.from_state is not None or first.to_state != OrderState.ACCEPTED:
        raise BrokerStateCorrupt(
            f"{source}: first history entry must be None -> accepted, got "
            f"{first.from_state} -> {first.to_state.value}"
        )
    prev = first
    for i in range(1, len(history)):
        entry = history[i]
        if entry.from_state is None or entry.from_state != prev.to_state:
            raise BrokerStateCorrupt(
                f"{source}: history[{i}].from_state={entry.from_state} does not "
                f"match previous to_state={prev.to_state.value}"
            )
        allowed = VALID_TRANSITIONS.get(entry.from_state, frozenset())
        if entry.to_state not in allowed:
            raise BrokerStateCorrupt(
                f"{source}: history[{i}] transition "
                f"{entry.from_state.value} -> {entry.to_state.value} is not "
                f"permitted"
            )
        if entry.at_utc < prev.at_utc:
            raise BrokerStateCorrupt(
                f"{source}: history timestamps must be nondecreasing "
                f"(index {i}: {entry.at_utc.isoformat()} < "
                f"{prev.at_utc.isoformat()})"
            )
        prev = entry
    if history[-1].to_state != final_state:
        raise BrokerStateCorrupt(
            f"{source}: state {final_state.value} does not match last history "
            f"entry {history[-1].to_state.value}"
        )


__all__ = [
    "SCHEMA_VERSION",
    "BrokerError",
    "BrokerStateCorrupt",
    "FillExceedsIntent",
    "InvalidStateTransition",
    "MockBroker",
    "deterministic_client_order_id",
]
