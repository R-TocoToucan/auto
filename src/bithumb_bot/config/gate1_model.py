"""Gate-1 frozen decision model + `StrictDecimal` type.

D-58: parse TOML separately, then validate through an immutable/frozen
pydantic v2 model. Do NOT use `pydantic.BaseSettings` here — that is for
env-var-sourced operational configuration, not committed TOML.

D-49 / Finding 3: every decimal-bearing field must be authored as a QUOTED
TOML string. A TOML numeric literal (int OR float) is rejected on load —
this is the whole point of `StrictDecimal`.

D-60: any structural problem (unknown key, unfrozen status, mis-typed
value) is a fail-closed condition — pydantic raises `ValidationError`.

--- On the value-deferred sentinel ---
Fields whose schema is frozen at Gate 1 but whose numeric value is frozen
at Gate 2 (D-41) or Gate 3 (D-42), plus D-09's `max_validated_notional_krw`,
are authored in `config/decisions/gate1.toml` as inline tables of shape
`{ value = "unset", frozen_at = "gateN", frozen_at_phase = N }` (see the
TOML file's header — TOML 1.0.0 has no `null` literal). This module's
`_optional_strict_decimal_from_input` normalises that sentinel to Python
`None`; a real value is a quoted decimal string on both TOML sides once
the value freezes at its downstream gate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict


# =============================================================================
# StrictDecimal
# =============================================================================


def _decimal_from_str(value: object) -> Decimal:
    """BeforeValidator for `StrictDecimal`.

    Accepts only:
      * `str`     — parsed via `Decimal(value)`
      * `Decimal` — passed through untouched

    Rejects `float`, `int`, `bool`, `None`, and every other type — the
    project's whole point is to force operators to author monetary values
    as unambiguous quoted strings at the TOML source level.

    Note: `bool` is a subclass of `int` in Python, so the `int` branch
    below also catches booleans; the explicit `bool` check is defensive
    documentation.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    # ValueError is what pydantic v2 wraps into ValidationError. Raising
    # TypeError here would escape pydantic's validator error-handling and
    # bypass the fail-closed contract the caller relies on (D-60).
    raise ValueError(
        f"StrictDecimal fields must be TOML strings, got {type(value).__name__!r}: "
        f"{value!r}. Author the value as a QUOTED TOML string (e.g. \"100000\") "
        "so it cannot be silently coerced from a float literal."
    )


StrictDecimal = Annotated[Decimal, BeforeValidator(_decimal_from_str)]
"""Annotated Decimal type that ONLY accepts a `str` (or existing Decimal)."""


# =============================================================================
# Optional-StrictDecimal (value-deferred sentinel handling)
# =============================================================================


_UNSET_SENTINEL_MARKER = "unset"


def _optional_strict_decimal_from_input(value: object) -> Decimal | None:
    """BeforeValidator for `OptionalStrictDecimal`.

    Accepts:
      * `None`                                            — pass-through
      * `Decimal`                                         — pass-through
      * `str`                                             — parsed via `Decimal(...)`
      * inline-table sentinel `{ "value": "unset", ... }` — normalised to None

    Rejects everything else with a `TypeError` (same discipline as
    `_decimal_from_str`).
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, dict):
        marker = value.get("value")
        if marker == _UNSET_SENTINEL_MARKER:
            return None
        raise ValueError(
            "value-deferred inline table must have value = \"unset\"; got "
            f"{value!r}"
        )
    raise ValueError(
        f"OptionalStrictDecimal fields must be a quoted string, an unset-sentinel "
        f"inline table, or explicitly null; got {type(value).__name__!r}: {value!r}"
    )


OptionalStrictDecimal = Annotated[
    Decimal | None, BeforeValidator(_optional_strict_decimal_from_input)
]
"""Annotated Optional-Decimal that recognises the `{ value = "unset", ... }` sentinel."""


# =============================================================================
# Optional-Int / Optional-Str with the same sentinel handling
# =============================================================================


def _optional_int_from_input(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass — reject explicitly to avoid True == 1 surprises
        raise ValueError(
            f"OptionalInt fields reject bool; got {value!r}"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        marker = value.get("value")
        if marker == _UNSET_SENTINEL_MARKER:
            return None
        raise ValueError(
            "value-deferred inline table must have value = \"unset\"; got "
            f"{value!r}"
        )
    raise ValueError(
        f"OptionalInt fields must be an int, an unset-sentinel inline table, or "
        f"explicitly null; got {type(value).__name__!r}: {value!r}"
    )


OptionalInt = Annotated[int | None, BeforeValidator(_optional_int_from_input)]


def _optional_str_from_input(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        marker = value.get("value")
        if marker == _UNSET_SENTINEL_MARKER:
            return None
        raise ValueError(
            "value-deferred inline table must have value = \"unset\"; got "
            f"{value!r}"
        )
    raise ValueError(
        f"OptionalStr fields must be a string, an unset-sentinel inline table, or "
        f"explicitly null; got {type(value).__name__!r}: {value!r}"
    )


OptionalStr = Annotated[str | None, BeforeValidator(_optional_str_from_input)]


# =============================================================================
# Gate1Decisions
# =============================================================================


class Gate1Decisions(BaseModel):
    """Frozen pydantic v2 model of the Gate-1 Decision Register.

    Field set exactly mirrors the top-level keys of
    `config/decisions/gate1.toml`. `frozen=True` makes instances immutable
    (attribute assignment raises `ValidationError`); `extra="forbid"`
    rejects any TOML key not declared here; `strict=True` prevents lax
    coercion (which would defeat the `StrictDecimal` discipline).
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    # --- Provenance (D-61) ---
    schema_version: Literal[1]
    status: Literal["frozen"]
    approved_at_utc: str
    source_commit: str
    research_spec_sha256: str
    execution_spec_sha256: str

    # --- D-40: M1 read-only HTTP (frozen) ---
    m1_spec_http_connect_timeout_ms: int
    m1_spec_http_read_timeout_ms: int
    m1_spec_http_max_attempts: int
    m1_spec_http_backoff_initial_ms: int
    m1_spec_http_backoff_cap_ms: int
    m1_spec_http_jitter: Literal["full"]

    # --- D-09 / D-43: applicability cap fields ---
    max_validated_notional_krw: OptionalStrictDecimal = None  # value-deferred to Gate 2
    provisional_engineering_notional_krw: StrictDecimal  # test-fixture-only value

    # --- D-41: Gate-2 schema-frozen, value-deferred ---
    max_received_trade_delivery_lag_ms: OptionalInt = None
    public_ws_transport_liveness_timeout_ms: OptionalInt = None
    fallback_rest_poll_interval_ms: OptionalInt = None
    trigger_rest_connect_timeout_ms: OptionalInt = None
    trigger_rest_read_timeout_ms: OptionalInt = None
    max_unverified_interval_ms: OptionalInt = None
    ws_recovery_stability_window_ms: OptionalInt = None
    ws_recovery_min_valid_events: OptionalInt = None

    # --- D-42: Gate-3 schema-frozen, value-deferred ---
    watchdog_heartbeat_interval_ms: OptionalInt = None
    watchdog_lease_ttl_ms: OptionalInt = None
    ws_reconnect_backoff_initial_ms: OptionalInt = None
    ws_reconnect_backoff_cap_ms: OptionalInt = None
    ws_reconnect_jitter_policy: OptionalStr = None
    per_reconnect_cycle_attempt_limit: OptionalInt = None
    reconnect_circuit_breaker_window_ms: OptionalInt = None


__all__ = [
    "Gate1Decisions",
    "OptionalInt",
    "OptionalStr",
    "OptionalStrictDecimal",
    "StrictDecimal",
    "_decimal_from_str",
    "_optional_strict_decimal_from_input",
]
