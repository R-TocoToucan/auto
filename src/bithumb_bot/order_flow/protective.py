"""Restart-safe runtime protective-stop dispatcher.

Bridges an observed live price crossing the protective-stop level into
the existing protective ``OrderIntent`` surface and submits it through
the venue-agnostic ``Broker`` protocol. The FIRST valid trigger is
persisted (canonical JSON + SHA-256 sidecar) BEFORE any broker mutation
so a restart between persistence and successful submit reconstructs the
same deterministic order and retries exactly once — never a second
protective order for the same stop.

Scope is deliberately narrow:

* No strategy math and no dataset walk (that is
  :func:`bithumb_bot.execution.stop.evaluate_protective_stop`'s job).
* No engine-side accounting (fee/slippage/tick/step). The protective
  ``OrderIntent`` carries the observed trigger price only; the engine
  owns everything past the intent boundary.
* Never calls ``MockBroker``-only mutation methods (``record_fill`` /
  ``cancel`` / ``reject``).
* No import of ``bithumb_bot.broker.mock``; only the venue-neutral
  identity helper, the ``Broker`` protocol, and broker state types are
  referenced.
* No network, credential, CLI, strategy, paper, or live imports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from bithumb_bot.artifact.canonical import (
    canonical_bytes,
    sha256_hex,
    write_with_sidecar,
)
from bithumb_bot.broker.identity import (
    canonical_decimal,
    deterministic_client_order_id,
)
from bithumb_bot.broker.interface import Broker
from bithumb_bot.broker.state import BrokerOrder, OrderState
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent, Reason
from bithumb_bot.execution.stop import ProtectiveStop

SCHEMA_VERSION = 1
_TRIGGERS_DIRNAME = "protective_triggers"
_VALID_REASONS: frozenset[str] = frozenset(
    {"protective_stop_gap", "protective_stop_intrabar"}
)
_TRIGGER_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "stop_id",
        "reason",
        "source_open_time_utc",
        "trigger_ts_utc",
        "trigger_price",
        "client_order_id",
        "market",
        "stop_qty",
        "unit_minutes",
    }
)


class ProtectiveDispatchError(Exception):
    """Base error for the runtime protective-stop dispatcher."""


class ProtectiveDispatchRefused(ProtectiveDispatchError):
    """Fail-closed refusal made BEFORE any broker mutation."""


class ProtectiveTriggerRecordCorrupt(ProtectiveDispatchError):
    """Persisted first-trigger record failed schema/integrity checks.

    Raised BEFORE any broker mutation. The on-disk record file and its
    sidecar are NEVER modified as a side effect of the refusal — they
    remain byte-identical so an operator can inspect the exact bytes.
    """


class ProtectiveOrderTerminalError(ProtectiveDispatchError):
    """Persisted protective order reached a non-fill terminal state.

    Fires when the recovered order is already ``canceled`` or
    ``rejected``. The dispatcher NEVER silently submits a replacement
    — operator intervention is required.
    """


@dataclass(frozen=True)
class StopObservation:
    """Immutable observation of a live price inside a candle window."""

    market: str
    candle_open_time_utc: datetime
    unit_minutes: int
    observed_at_utc: datetime
    candle_open_price: Money
    observed_price: Money


@dataclass(frozen=True)
class ProtectiveDispatchResult:
    """Immutable result of one :func:`dispatch_protective_stop` call.

    ``recovered_from_persisted_trigger`` is ``True`` when the dispatcher
    read an already-persisted first-trigger record instead of writing a
    new one — i.e., the trigger was persisted on a previous call (or in
    a previous process) and this call only recovered and re-submitted.
    """

    triggered: bool
    order: BrokerOrder | None
    stopped_out_lockout: bool
    recovered_from_persisted_trigger: bool


def dispatch_protective_stop(
    *,
    observation: StopObservation,
    stop: ProtectiveStop,
    position: Qty,
    state_dir: Path,
    broker: Broker,
) -> ProtectiveDispatchResult:
    """Convert an observed trigger into a persisted, idempotent protective sell.

    Order of operations:

    1. Fail-closed validation of the observation, stop, and position
       (position finiteness/sign only — quantity sufficiency is checked
       inside the branch that would actually submit).
    2. Load any persisted first-trigger record for this stop identity.
    3. If a persisted record exists: reconstruct the same
       ``OrderIntent``, verify its ``client_order_id`` matches the
       persisted one, then look up the broker order:

       * If the broker already holds an order for that ``client_order_id``
         with the exact reconstructed intent, map its state to a lockout
         verdict (accepted → no lockout; partially_filled/filled →
         lockout; canceled/rejected → :class:`ProtectiveOrderTerminalError`)
         and return WITHOUT submitting again.
       * If the broker holds no such order (crash between persist and
         first successful submit), require ``position >= stop.qty``,
         refuse if a different unresolved order is present, and then
         submit exactly once.

    4. Otherwise classify the observation. If it does not trigger,
       return a no-op result. If it triggers, require ``position >=
       stop.qty``, write the first-trigger record (canonical JSON +
       SHA-256 sidecar) BEFORE calling ``Broker.submit``, and submit.
    """
    _validate_all(observation, stop, position)

    triggers_dir = state_dir / _TRIGGERS_DIRNAME
    stop_id = _compute_stop_id(stop, observation.market)

    persisted = _load_trigger_record(
        triggers_dir,
        stop_id,
        expected_market=observation.market,
        stop=stop,
    )
    if persisted is not None:
        intent = _reconstruct_intent(persisted, stop)
        computed_cid = deterministic_client_order_id(intent)
        if computed_cid != persisted["client_order_id"]:
            raise ProtectiveTriggerRecordCorrupt(
                f"persisted client_order_id {persisted['client_order_id']!r} "
                f"does not hash to reconstructed intent (got {computed_cid!r})"
            )
        return _recover_persisted(broker, intent, position, stop)

    classification = _classify(observation, stop)
    if classification is None:
        return ProtectiveDispatchResult(
            triggered=False,
            order=None,
            stopped_out_lockout=False,
            recovered_from_persisted_trigger=False,
        )
    _require_position_ge_stop_qty(position, stop)
    reason, trigger_ts, trigger_price = classification
    intent = OrderIntent.protective_sell(
        reason=reason,
        qty=stop.qty,
        trigger_ts_utc=trigger_ts,
        trigger_price=trigger_price,
        source_open_time_utc=observation.candle_open_time_utc,
        unit_minutes=observation.unit_minutes,
    )
    cid = deterministic_client_order_id(intent)
    _write_trigger_record(
        triggers_dir=triggers_dir,
        stop_id=stop_id,
        reason=reason,
        source_open_time_utc=observation.candle_open_time_utc,
        trigger_ts_utc=trigger_ts,
        trigger_price=trigger_price,
        client_order_id=cid,
        market=observation.market,
        stop_qty=stop.qty,
        unit_minutes=observation.unit_minutes,
    )
    _refuse_if_different_unresolved(broker, intent)
    order = broker.submit(intent)
    return _map_submitted_order(order, recovered=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_tz_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def _require_exact_decimal(value: Decimal, name: str) -> None:
    if type(value) is not Decimal:  # noqa: E721 — explicit type identity
        raise ProtectiveDispatchRefused(
            f"{name} must be an exact Decimal, got {type(value).__name__!r}"
        )
    if not value.is_finite():
        raise ProtectiveDispatchRefused(f"{name} must be finite, got {value}")


def _require_finite_positive_money(value: Money, name: str) -> None:
    _require_exact_decimal(value.value, name)
    if value.value <= 0:
        raise ProtectiveDispatchRefused(f"{name} must be > 0, got {value.value}")


def _require_exact_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtectiveDispatchRefused(
            f"{name} must be int (not bool), got {type(value).__name__}"
        )
    if value <= 0:
        raise ProtectiveDispatchRefused(f"{name} must be > 0, got {value}")


def _validate_all(
    observation: StopObservation,
    stop: ProtectiveStop,
    position: Qty,
) -> None:
    if type(observation.market) is not str or not observation.market:  # noqa: E721
        raise ProtectiveDispatchRefused(
            f"market must be a non-empty str, got {observation.market!r}"
        )
    _require_exact_positive_int(observation.unit_minutes, "observation.unit_minutes")
    _require_exact_positive_int(stop.unit_minutes, "stop.unit_minutes")
    if observation.unit_minutes != stop.unit_minutes:
        raise ProtectiveDispatchRefused(
            f"observation.unit_minutes={observation.unit_minutes} must equal "
            f"stop.unit_minutes={stop.unit_minutes}"
        )
    for name, dt in (
        ("observation.candle_open_time_utc", observation.candle_open_time_utc),
        ("observation.observed_at_utc", observation.observed_at_utc),
        ("stop.activated_at_utc", stop.activated_at_utc),
    ):
        if not _is_tz_aware(dt):
            raise ProtectiveDispatchRefused(
                f"{name} must be timezone-aware, got {dt.isoformat()!r}"
            )
    close_ts = observation.candle_open_time_utc + timedelta(
        minutes=observation.unit_minutes
    )
    if not (
        observation.candle_open_time_utc
        <= observation.observed_at_utc
        < close_ts
    ):
        raise ProtectiveDispatchRefused(
            f"observed_at_utc {observation.observed_at_utc.isoformat()} must "
            f"satisfy candle_open <= observed_at < candle_close "
            f"[{observation.candle_open_time_utc.isoformat()}, "
            f"{close_ts.isoformat()})"
        )
    if observation.candle_open_time_utc < stop.activated_at_utc:
        raise ProtectiveDispatchRefused(
            f"stop not active for candle at "
            f"{observation.candle_open_time_utc.isoformat()} "
            f"(activated_at_utc={stop.activated_at_utc.isoformat()})"
        )
    _require_finite_positive_money(observation.candle_open_price, "candle_open_price")
    _require_finite_positive_money(observation.observed_price, "observed_price")
    _require_finite_positive_money(stop.stop_price, "stop.stop_price")
    _require_finite_positive_money(stop.entry_fill_price, "stop.entry_fill_price")
    _require_exact_decimal(stop.qty.value, "stop.qty")
    if stop.qty.value <= 0:
        raise ProtectiveDispatchRefused(
            f"stop.qty must be > 0, got {stop.qty.value}"
        )
    _require_exact_decimal(position.value, "position")
    if position.value < 0:
        raise ProtectiveDispatchRefused(
            f"position must be >= 0, got {position.value}"
        )
    if observation.observed_at_utc == observation.candle_open_time_utc:
        if observation.observed_price.value != observation.candle_open_price.value:
            raise ProtectiveDispatchRefused(
                f"observed_price {observation.observed_price.value} must equal "
                f"candle_open_price {observation.candle_open_price.value} when "
                f"observed_at equals candle open"
            )


def _require_position_ge_stop_qty(position: Qty, stop: ProtectiveStop) -> None:
    if position.value < stop.qty.value:
        raise ProtectiveDispatchRefused(
            f"position {position.value} must be >= stop.qty {stop.qty.value}"
        )


# ---------------------------------------------------------------------------
# Trigger classification
# ---------------------------------------------------------------------------


def _classify(
    observation: StopObservation,
    stop: ProtectiveStop,
) -> tuple[Reason, datetime, Money] | None:
    """Return ``(reason, trigger_ts, trigger_price)`` or ``None``.

    Rules (per the runtime dispatcher spec):

    * ``observed_price > stop.stop_price`` never triggers.
    * ``observed_at == candle_open`` on the activation candle never
      triggers (the stop did not exist before that open).
    * ``observed_at == candle_open`` on a strictly-later candle with
      ``candle_open_price <= stop.stop_price`` triggers as a gap; the
      trigger price is the observed candle-open price.
    * ``observed_at`` strictly inside the candle with
      ``observed_price <= stop.stop_price`` triggers as intrabar; the
      trigger price is the stop-level price (predeclared worse-outcome
      convention matching ``evaluate_protective_stop``).
    """
    if observation.observed_price.value > stop.stop_price.value:
        return None
    is_activation_candle = (
        observation.candle_open_time_utc == stop.activated_at_utc
    )
    if observation.observed_at_utc == observation.candle_open_time_utc:
        if is_activation_candle:
            return None
        return (
            "protective_stop_gap",
            observation.candle_open_time_utc,
            observation.candle_open_price,
        )
    return (
        "protective_stop_intrabar",
        observation.observed_at_utc,
        stop.stop_price,
    )


# ---------------------------------------------------------------------------
# Stop identity + trigger persistence
# ---------------------------------------------------------------------------


def _compute_stop_id(stop: ProtectiveStop, market: str) -> str:
    payload = {
        "market": market,
        "stop_price": canonical_decimal(stop.stop_price.value),
        "qty": canonical_decimal(stop.qty.value),
        "activated_at_utc": stop.activated_at_utc.astimezone(UTC).isoformat(),
        "unit_minutes": stop.unit_minutes,
        "entry_fill_price": canonical_decimal(stop.entry_fill_price.value),
    }
    return sha256_hex(canonical_bytes(payload))


def _trigger_payload(
    *,
    stop_id: str,
    reason: Reason,
    source_open_time_utc: datetime,
    trigger_ts_utc: datetime,
    trigger_price: Money,
    client_order_id: str,
    market: str,
    stop_qty: Qty,
    unit_minutes: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "stop_id": stop_id,
        "reason": reason,
        "source_open_time_utc": source_open_time_utc.astimezone(UTC).isoformat(),
        "trigger_ts_utc": trigger_ts_utc.astimezone(UTC).isoformat(),
        "trigger_price": str(trigger_price.value),
        "client_order_id": client_order_id,
        "market": market,
        "stop_qty": str(stop_qty.value),
        "unit_minutes": unit_minutes,
    }


def _write_trigger_record(
    *,
    triggers_dir: Path,
    stop_id: str,
    reason: Reason,
    source_open_time_utc: datetime,
    trigger_ts_utc: datetime,
    trigger_price: Money,
    client_order_id: str,
    market: str,
    stop_qty: Qty,
    unit_minutes: int,
) -> None:
    triggers_dir.mkdir(parents=True, exist_ok=True)
    payload = _trigger_payload(
        stop_id=stop_id,
        reason=reason,
        source_open_time_utc=source_open_time_utc,
        trigger_ts_utc=trigger_ts_utc,
        trigger_price=trigger_price,
        client_order_id=client_order_id,
        market=market,
        stop_qty=stop_qty,
        unit_minutes=unit_minutes,
    )
    target = triggers_dir / f"{stop_id}.json"
    write_with_sidecar(target, canonical_bytes(payload))


def _load_trigger_record(
    triggers_dir: Path,
    stop_id: str,
    *,
    expected_market: str,
    stop: ProtectiveStop,
) -> dict[str, Any] | None:
    target = triggers_dir / f"{stop_id}.json"
    sidecar = target.with_name(f"{target.name}.sha256")
    target_exists = target.is_file()
    sidecar_exists = sidecar.is_file()
    if not target_exists:
        if sidecar_exists:
            raise ProtectiveTriggerRecordCorrupt(
                f"orphan sidecar without JSON record for stop_id={stop_id!r}: "
                f"{sidecar}"
            )
        return None
    if not sidecar_exists:
        raise ProtectiveTriggerRecordCorrupt(
            f"missing sidecar for trigger record {target}"
        )
    raw = target.read_bytes()
    on_disk_hex = sha256_hex(raw)
    sidecar_bytes = sidecar.read_bytes()
    expected_sidecar = f"{on_disk_hex}  {target.name}\n".encode("utf-8")
    if sidecar_bytes != expected_sidecar:
        raise ProtectiveTriggerRecordCorrupt(
            f"sidecar bytes must be exactly '<hex>  {target.name}\\n' with "
            f"hash matching JSON; got {sidecar_bytes!r} expected "
            f"{expected_sidecar!r}"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtectiveTriggerRecordCorrupt(
            f"invalid JSON in {target}: {exc}"
        ) from exc
    recanon = canonical_bytes(data)
    if raw != recanon:
        raise ProtectiveTriggerRecordCorrupt(
            f"{target}: JSON bytes are not canonical (sort_keys, compact "
            f"separators, trailing newline)"
        )
    _validate_trigger_payload(
        data,
        expected_stop_id=stop_id,
        expected_market=expected_market,
        stop=stop,
        source=target,
    )
    return cast(dict[str, Any], data)


def _validate_trigger_payload(
    data: Any,
    *,
    expected_stop_id: str,
    expected_market: str,
    stop: ProtectiveStop,
    source: Path,
) -> None:
    if not isinstance(data, dict):
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: top-level payload is not a JSON object"
        )
    keys = set(data.keys())
    if keys != _TRIGGER_RECORD_KEYS:
        missing = _TRIGGER_RECORD_KEYS - keys
        extra = keys - _TRIGGER_RECORD_KEYS
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: schema keys mismatch (missing={sorted(missing)!r}, "
            f"extra={sorted(extra)!r})"
        )
    schema = data["schema_version"]
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: schema_version must be int, got "
            f"{type(schema).__name__}"
        )
    if schema != SCHEMA_VERSION:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: unsupported schema_version={schema!r} "
            f"(expected {SCHEMA_VERSION})"
        )
    stop_id = data["stop_id"]
    if not isinstance(stop_id, str) or stop_id != expected_stop_id:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: stop_id mismatch (file={expected_stop_id!r} "
            f"payload={stop_id!r})"
        )
    reason = data["reason"]
    if not isinstance(reason, str) or reason not in _VALID_REASONS:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: reason invalid: {reason!r}"
        )
    for key in ("source_open_time_utc", "trigger_ts_utc"):
        value = data[key]
        if not isinstance(value, str):
            raise ProtectiveTriggerRecordCorrupt(
                f"{source}: {key} must be a string, got "
                f"{type(value).__name__}"
            )
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ProtectiveTriggerRecordCorrupt(
                f"{source}: {key} not ISO-parsable: {value!r}"
            ) from exc
        if not _is_tz_aware(parsed):
            raise ProtectiveTriggerRecordCorrupt(
                f"{source}: {key} must be timezone-aware, got {value!r}"
            )
    _decode_decimal_str(
        data["trigger_price"], field="trigger_price", source=source
    )
    stop_qty_dec = _decode_decimal_str(
        data["stop_qty"], field="stop_qty", source=source
    )
    if stop_qty_dec != stop.qty.value:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: stop_qty {stop_qty_dec} does not match current "
            f"stop.qty {stop.qty.value}"
        )
    cid = data["client_order_id"]
    if not isinstance(cid, str) or len(cid) != 64 or not all(
        c in "0123456789abcdef" for c in cid
    ):
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: client_order_id must be 64-char lowercase hex, "
            f"got {cid!r}"
        )
    market = data["market"]
    if not isinstance(market, str) or not market:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: market must be a non-empty string, got {market!r}"
        )
    if market != expected_market:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: market mismatch (expected {expected_market!r}, "
            f"payload {market!r})"
        )
    unit = data["unit_minutes"]
    if isinstance(unit, bool) or not isinstance(unit, int) or unit <= 0:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: unit_minutes must be a positive int, got {unit!r}"
        )
    if unit != stop.unit_minutes:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: unit_minutes {unit} does not match current "
            f"stop.unit_minutes {stop.unit_minutes}"
        )


def _decode_decimal_str(value: Any, *, field: str, source: Path) -> Decimal:
    if not isinstance(value, str):
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: {field} must be a JSON string, got "
            f"{type(value).__name__}"
        )
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: {field} is not a valid decimal string: {value!r}"
        ) from exc
    if not d.is_finite() or d <= 0:
        raise ProtectiveTriggerRecordCorrupt(
            f"{source}: {field} must be a finite positive decimal, got {value!r}"
        )
    return d


# ---------------------------------------------------------------------------
# Intent reconstruction + broker submission
# ---------------------------------------------------------------------------


def _reconstruct_intent(
    record: dict[str, Any], stop: ProtectiveStop
) -> OrderIntent:
    reason = cast(Reason, record["reason"])
    trigger_ts = datetime.fromisoformat(record["trigger_ts_utc"])
    source_open_time = datetime.fromisoformat(record["source_open_time_utc"])
    trigger_price = Money(Decimal(record["trigger_price"]))
    try:
        return OrderIntent.protective_sell(
            reason=reason,
            qty=stop.qty,
            trigger_ts_utc=trigger_ts,
            trigger_price=trigger_price,
            source_open_time_utc=source_open_time,
            unit_minutes=stop.unit_minutes,
        )
    except ValueError as exc:
        raise ProtectiveTriggerRecordCorrupt(
            f"persisted trigger record fails OrderIntent invariants: {exc}"
        ) from exc


def _refuse_if_different_unresolved(
    broker: Broker, intent: OrderIntent
) -> None:
    for existing in broker.list_open():
        if existing.intent != intent:
            raise ProtectiveDispatchRefused(
                f"broker holds unresolved order "
                f"{existing.client_order_id} with a different intent; "
                f"refuse to submit protective order"
            )


def _map_submitted_order(
    order: BrokerOrder, *, recovered: bool
) -> ProtectiveDispatchResult:
    if order.state in (OrderState.CANCELED, OrderState.REJECTED):
        raise ProtectiveOrderTerminalError(
            f"protective order {order.client_order_id} is in terminal "
            f"non-fill state {order.state.value}; operator intervention "
            f"required"
        )
    lockout = order.state in (
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    )
    return ProtectiveDispatchResult(
        triggered=True,
        order=order,
        stopped_out_lockout=lockout,
        recovered_from_persisted_trigger=recovered,
    )


def _recover_persisted(
    broker: Broker,
    intent: OrderIntent,
    position: Qty,
    stop: ProtectiveStop,
) -> ProtectiveDispatchResult:
    cid = deterministic_client_order_id(intent)
    existing = broker.get(cid)
    if existing is not None:
        if existing.intent != intent:
            raise ProtectiveTriggerRecordCorrupt(
                f"broker order {cid} intent does not match reconstructed "
                f"protective intent"
            )
        return _map_submitted_order(existing, recovered=True)
    # Persisted trigger without a matching broker order — a crash between
    # persistence and the first successful submit. Guard against an
    # oversize sell if the position has since been reduced by an
    # unrelated action.
    _require_position_ge_stop_qty(position, stop)
    _refuse_if_different_unresolved(broker, intent)
    order = broker.submit(intent)
    return _map_submitted_order(order, recovered=True)


__all__ = [
    "SCHEMA_VERSION",
    "ProtectiveDispatchError",
    "ProtectiveDispatchRefused",
    "ProtectiveDispatchResult",
    "ProtectiveOrderTerminalError",
    "ProtectiveTriggerRecordCorrupt",
    "StopObservation",
    "dispatch_protective_stop",
]
