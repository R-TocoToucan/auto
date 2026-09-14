"""Venue-neutral deterministic order identity.

Canonicalizes an :class:`~bithumb_bot.execution.intent.OrderIntent` to
bytes and derives a SHA-256 ``client_order_id`` from those bytes. Both
the persistent ``MockBroker`` and the protective-stop dispatcher depend
on the SAME canonical form so a persisted intent, its stored
``client_order_id``, and any later reconstruction agree exactly.

Design rules:

1. Datetimes are normalized to UTC before hashing — two intents that
   refer to the same instant in different offsets hash identically.
2. Decimals are canonicalized via ``as_tuple()`` so numerically-equal
   values fold together (``Decimal("100000")`` and
   ``Decimal("100000.00")`` collapse to the same string), while
   distinct high-precision values NEVER collide (no context-dependent
   rounding via ``.normalize()``).
3. Non-finite Decimals and naive datetimes are refused fail-closed —
   they cannot be canonicalized safely and must not reach disk.
4. No I/O, no HTTP, no environment access — pure functions only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from bithumb_bot.execution.intent import OrderIntent


def canonical_datetime(dt: datetime) -> str:
    """UTC-normalized ISO-8601 string. Naive datetimes are refused."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"naive datetime not permitted: {dt.isoformat()}")
    return dt.astimezone(UTC).isoformat()


def canonical_decimal(value: Decimal) -> str:
    """Canonical string form derived directly from ``Decimal.as_tuple()``.

    Numerically-equal values collapse to the same string
    (``Decimal("100000")`` and ``Decimal("100000.00")`` both fold to
    ``+1E5``); distinct arbitrary-precision values NEVER round together
    because no context-dependent normalization is applied.
    Non-finite Decimals are refused.
    """
    if not value.is_finite():
        raise ValueError(f"non-finite Decimal not permitted: {value}")
    tup = value.as_tuple()
    digits: tuple[int, ...] = tup.digits
    exponent = tup.exponent
    if not isinstance(exponent, int):
        raise ValueError(f"non-finite Decimal exponent: {exponent!r}")
    if all(d == 0 for d in digits):
        return "+0E0"
    end = len(digits)
    while end > 1 and digits[end - 1] == 0:
        end -= 1
        exponent += 1
    coeff = "".join(str(d) for d in digits[:end])
    sign_char = "-" if tup.sign == 1 else "+"
    return f"{sign_char}{coeff}E{exponent}"


def canonical_intent_bytes(intent: OrderIntent) -> bytes:
    """Deterministic byte-for-byte serialization used ONLY for hashing.

    Two intents whose fields are equal after datetime and decimal
    canonicalization always produce equal bytes here.
    """
    payload = {
        "side": intent.side,
        "source_open_time_utc": canonical_datetime(intent.source_open_time_utc),
        "unit_minutes": intent.unit_minutes,
        "signal_ts_utc": canonical_datetime(intent.signal_ts_utc),
        "requested_notional_krw": (
            canonical_decimal(intent.requested_notional_krw.value)
            if intent.requested_notional_krw is not None
            else None
        ),
        "requested_qty": (
            canonical_decimal(intent.requested_qty.value)
            if intent.requested_qty is not None
            else None
        ),
        "reason": intent.reason,
        "trigger_price": (
            canonical_decimal(intent.trigger_price.value)
            if intent.trigger_price is not None
            else None
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def deterministic_client_order_id(intent: OrderIntent) -> str:
    """SHA-256 hex (64 chars) of the canonical intent bytes.

    Raises ``ValueError`` if the intent carries a naive datetime or a
    non-finite Decimal — those inputs cannot be canonicalized safely.
    """
    return hashlib.sha256(canonical_intent_bytes(intent)).hexdigest()


WIRE_CLIENT_ORDER_ID_PREFIX = "bt-"
WIRE_CLIENT_ORDER_ID_HASH_LEN = 33
WIRE_CLIENT_ORDER_ID_TOTAL_LEN = (
    len(WIRE_CLIENT_ORDER_ID_PREFIX) + WIRE_CLIENT_ORDER_ID_HASH_LEN
)  # 36 — the Bithumb venue cap.


def deterministic_wire_client_order_id(intent: OrderIntent) -> str:
    """Return the venue-safe ``client_order_id`` sent on the wire.

    Bithumb caps ``client_order_id`` at 36 characters. The internal
    persisted id is the full 64-char SHA-256 (collision guard); the
    wire id is a deterministic function of the same canonical bytes —
    the ``bt-`` prefix (venue-side filter) followed by the first 33 hex
    chars (132 bits of collision resistance).

    Same-intent-in → same-string-out, so a persisted mapping can be
    rehashed and matched byte-for-byte after restart.
    """
    return (
        f"{WIRE_CLIENT_ORDER_ID_PREFIX}"
        f"{deterministic_client_order_id(intent)[:WIRE_CLIENT_ORDER_ID_HASH_LEN]}"
    )


__all__ = [
    "WIRE_CLIENT_ORDER_ID_HASH_LEN",
    "WIRE_CLIENT_ORDER_ID_PREFIX",
    "WIRE_CLIENT_ORDER_ID_TOTAL_LEN",
    "canonical_datetime",
    "canonical_decimal",
    "canonical_intent_bytes",
    "deterministic_client_order_id",
    "deterministic_wire_client_order_id",
]
