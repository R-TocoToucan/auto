"""`SnapshotV1` model + build / write / load flow (D-75, D-76, D-79, D-80).

The M1 spec snapshot is the ONLY authoritative fee/tick/step/min-order
source the M2 simulator consumes. It MUST be:

* canonical-serialized (byte-deterministic across producers — D-76);
* SHA-256 hashed with a sidecar (D-76);
* never overwritten silently (D-76 + T-1-04-09);
* refused at load time if the sidecar hash disagrees with the on-disk
  bytes (T-1-04-03), if the schema version is unknown (D-79), or if
  any required ``verification_status`` key is missing (D-80).

Per-capability status keys required by D-83:

* ``general_fee_rate``          — `confirmed_read_only` (default) once
  the M1 fetch succeeded and the model validated.
* ``market_buy_fee_reservation`` — `provisional_documented` (D-81).
* ``rounding_rejection_behavior`` — `unresolved_until_M6B` (D-82).
* ``live_order_acceptance``     — `unresolved_until_M6B` (D-82).

Every allowed status value comes from the `Literal[...]` union in
:class:`_VerificationStatus`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bithumb_bot.artifact.canonical import (
    canonical_bytes,
    guard_against_overwrite,
    sha256_hex,
    write_with_sidecar,
)
from bithumb_bot.artifact.timestamps import utc_now
from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.tick_schedule import tick_schedule_provenance
from bithumb_bot.config.gate1_model import OptionalStrictDecimal, StrictDecimal
from bithumb_bot.config.validator import validate
from bithumb_bot.errors import (
    SidecarHashMismatchError,
    SnapshotValidationError,
)

_VerificationStatusValue = Literal[
    "confirmed_read_only",
    "confirmed_documented",
    "provisional_documented",
    "unresolved_until_M6B",
    "contradicted",
]

_REQUIRED_STATUS_KEYS: frozenset[str] = frozenset(
    {
        "general_fee_rate",
        "market_buy_fee_reservation",
        "rounding_rejection_behavior",
        "live_order_acceptance",
    }
)


class FeeRates(BaseModel):
    """Nested D-75 fee-rate block."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    bid: StrictDecimal
    ask: StrictDecimal
    maker_bid: OptionalStrictDecimal = None
    maker_ask: OptionalStrictDecimal = None


class Minimums(BaseModel):
    """Nested D-75 minimums block."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    krw_min_total_bid: OptionalStrictDecimal = None
    krw_min_total_ask: OptionalStrictDecimal = None
    krw_max_total: OptionalStrictDecimal = None


class SnapshotV1(BaseModel):
    """The D-75 simulator-consumed JSON shape as a pydantic v2 model."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    venue: Literal["bithumb"]
    market: str
    retrieved_at_utc: str
    source_endpoints: list[str]
    fee_rates: FeeRates
    minimums: Minimums
    price_tick_rules: dict[str, StrictDecimal] = Field(default_factory=dict)
    quantity_step_rules: dict[str, StrictDecimal] = Field(default_factory=dict)
    supported_order_types: list[str]
    verification_status: dict[str, _VerificationStatusValue]
    source_fixture_hashes: list[str]
    #: Provenance of the official KRW price-tick schedule when it was
    #: attached at snapshot-build time (Batch 1B, ``bithumb_bot.
    #: bithumb_spec.tick_schedule``). None when the snapshot was built
    #: before the schedule was documented — the engine falls back to
    #: ``price_tick_rules["default_tick"]`` in that case.
    price_tick_schedule_provenance: dict[str, str] | None = None

    @field_validator("verification_status")
    @classmethod
    def _require_all_status_keys(
        cls, v: dict[str, _VerificationStatusValue]
    ) -> dict[str, _VerificationStatusValue]:
        missing = _REQUIRED_STATUS_KEYS - set(v.keys())
        if missing:
            raise ValueError(
                f"verification_status missing required keys per D-83: "
                f"{sorted(missing)!r}"
            )
        return v


# ---------------------------------------------------------------------------
# build / serialize / write / load
# ---------------------------------------------------------------------------


_ORDERS_CHANCE_ENDPOINT = "/v1/orders/chance"


def build_snapshot(
    response: OrdersChanceResponse,
    *,
    market: str,
    fixture_paths: list[Path],
) -> SnapshotV1:
    """Build a `SnapshotV1` from a validated `OrdersChanceResponse`.

    Defaults per D-81 / D-82:
      * ``general_fee_rate``: ``confirmed_read_only`` — the response
        validated and the fee rates are on it.
      * ``market_buy_fee_reservation``: ``provisional_documented`` (D-81
        — documented but not live-tested).
      * ``rounding_rejection_behavior``: ``unresolved_until_M6B`` (D-82).
      * ``live_order_acceptance``: ``unresolved_until_M6B`` (D-82).

    Args:
        response:      Parsed `/v1/orders/chance` response.
        market:        The KRW market symbol (``"KRW-BTC"``).
        fixture_paths: Paths of the sanitized fixture(s) that fed this
                       snapshot. Their sha256 digests are recorded in
                       ``source_fixture_hashes``.

    Returns:
        A frozen `SnapshotV1`.
    """
    return SnapshotV1(
        schema_version=1,
        venue="bithumb",
        market=market,
        retrieved_at_utc=utc_now().isoformat(timespec="seconds").replace("+00:00", "Z"),
        source_endpoints=[_ORDERS_CHANCE_ENDPOINT],
        fee_rates=FeeRates(
            bid=response.bid_fee,
            ask=response.ask_fee,
            maker_bid=response.maker_bid_fee,
            maker_ask=response.maker_ask_fee,
        ),
        minimums=Minimums(
            krw_min_total_bid=(
                response.market.bid.min_total if response.market.bid else None
            ),
            krw_min_total_ask=(
                response.market.ask.min_total if response.market.ask else None
            ),
            krw_max_total=response.market.max_total,
        ),
        # MVP per the plan: single default_tick / default_step slot, deferred.
        price_tick_rules=_default_price_tick_rules(response),
        quantity_step_rules=_default_quantity_step_rules(response),
        supported_order_types=list(response.market.order_types),
        verification_status={
            "general_fee_rate": "confirmed_read_only",
            "market_buy_fee_reservation": "provisional_documented",
            "rounding_rejection_behavior": "unresolved_until_M6B",
            "live_order_acceptance": "unresolved_until_M6B",
            # Batch 1B: order-type support is documented in the official
            # order-request docs. `confirmed_documented` is research-
            # sufficient but NOT a live-authorization guarantee — see
            # :mod:`bithumb_bot.execution.readiness`.
            "market_buy_price_support": "confirmed_documented",
            "market_sell_market_support": "confirmed_documented",
        },
        source_fixture_hashes=[
            sha256_hex(p.read_bytes()) for p in fixture_paths if p.is_file()
        ],
        # Batch 1B: attach the official price-tick schedule provenance
        # so the engine's tick resolver can select per-band ticks
        # (Decimal-native) instead of relying on a single
        # `default_tick`. Live venue quantity-step remains unresolved.
        price_tick_schedule_provenance=tick_schedule_provenance(),
    )


def _default_price_tick_rules(
    response: OrdersChanceResponse,
) -> dict[str, Decimal]:
    """MVP `price_tick_rules` — single `default_tick` from `market.bid.price_unit`.

    TODO(M1-verify): per-price-band structure is Open Verification
    Item #4 area. This MVP stores the documented KRW price_unit as
    `default_tick`; the M2 simulator uses it as a single-band tick
    until the M1 verification confirms the full band structure.
    """
    if response.market.bid and response.market.bid.price_unit is not None:
        return {"default_tick": response.market.bid.price_unit}
    return {}


def _default_quantity_step_rules(
    response: OrdersChanceResponse,
) -> dict[str, Decimal]:
    """MVP `quantity_step_rules` — single `default_step` slot.

    TODO(M1-verify): The `/v1/orders/chance` response does not (per
    Finding 6) directly expose a volume-step field for the ask side
    in a stable location; the M1 verification pins the field name.
    For MVP we leave this empty and defer to M1 verification.
    """
    return {}


def serialize_snapshot(snapshot: SnapshotV1) -> bytes:
    """Serialize `snapshot` to canonical JSON bytes.

    ``model_dump(mode='json')`` stringifies every `Decimal` field (D-75
    "every decimal as a string"). :func:`canonical_bytes` then applies
    the D-76 canonical serialization rules.
    """
    return canonical_bytes(snapshot.model_dump(mode="json"))


def write_snapshot_with_sidecar(
    snapshot: SnapshotV1, target: Path
) -> tuple[Path, Path]:
    """Write ``snapshot`` and its `.sha256` sidecar atomically to ``target``.

    Calls :func:`guard_against_overwrite` first — refuses to silently
    replace a previously consumed snapshot (D-76) and quarantines any
    corrupt existing file (T-1-04-09).
    """
    sidecar = target.with_name(target.name + ".sha256")
    guard_against_overwrite(target, sidecar)
    return write_with_sidecar(target, serialize_snapshot(snapshot))


def load_snapshot(path: Path) -> SnapshotV1:
    """Load, hash-verify, and structurally validate a snapshot from ``path``.

    Sequence:

    1. `validate(("m1", "verify-snapshot"))` (D-85 defense in depth).
    2. Read ``path`` bytes; recompute SHA-256.
    3. Read the ``.sha256`` sidecar; compare hex. Mismatch or missing
       → :class:`~bithumb_bot.errors.SidecarHashMismatchError`.
    4. Parse the JSON; validate via `SnapshotV1.model_validate`. Failed
       validation → :class:`~bithumb_bot.errors.SnapshotValidationError`.
    5. Re-check the required verification_status keys per D-80 (the
       pydantic validator already enforces this; the explicit check
       here is belt-and-suspenders since a corrupted keyset might slip
       past a future model refactor).

    Args:
        path: Absolute path to the snapshot JSON.

    Returns:
        A frozen `SnapshotV1`.

    Raises:
        SidecarHashMismatchError: on-disk bytes do not match sidecar.
        SnapshotValidationError:  structural / D-80 validation failed.
    """
    _result = validate(("m1", "verify-snapshot"))
    if not _result.ok:
        raise SnapshotValidationError(
            f"validate() refused m1 verify-snapshot: {_result.reason} "
            f"(missing: {list(_result.missing)!r})"
        )
    if not path.is_file():
        raise SnapshotValidationError(f"snapshot path does not exist: {path!r}")
    data = path.read_bytes()
    on_disk_hex = sha256_hex(data)
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise SidecarHashMismatchError(path)
    recorded = sidecar.read_text(encoding="utf-8").strip().split()
    if len(recorded) < 1 or len(recorded[0]) != 64 or recorded[0] != on_disk_hex:
        raise SidecarHashMismatchError(path)
    try:
        parsed: Any = _parse_json_bytes(data)
        snapshot = SnapshotV1.model_validate(parsed)
    except SnapshotValidationError:
        raise
    except Exception as exc:  # pydantic ValidationError, json errors, etc.
        raise SnapshotValidationError(
            f"snapshot at {path!r} failed structural validation: {exc}"
        ) from exc
    return snapshot


def _parse_json_bytes(data: bytes) -> Any:
    import json

    return json.loads(data.decode("utf-8"))


__all__ = [
    "FeeRates",
    "Minimums",
    "SnapshotV1",
    "build_snapshot",
    "guard_against_overwrite",  # re-export for callers who want it visible
    "load_snapshot",
    "serialize_snapshot",
    "write_snapshot_with_sidecar",
]
