"""Canonical persisted live-trading state (`live_state.json` + sidecar).

Single authoritative on-disk record for the one-cycle live-trading
coordinator. Replaces the earlier plain-text ``last_processed_open_time_utc``
marker with a strictly-validated, hash-attested state file that pins:

* schema identity (``schema_version``, ``strategy_id``, strategy constants,
  dataset market/unit);
* the exact snapshot and config the state was produced under
  (``snapshot_sha256`` and ``config_sha256``);
* an immutable fingerprint for every completed candle the strategy has
  ever consumed — a shortened / reordered / mutated prefix refuses at
  load time BEFORE any broker mutation;
* the bot-owned position (``bot_owned_position_qty`` /
  ``bot_owned_cost_basis_krw``) that is folded only from monotonic
  managed-order fill deltas — unrelated venue BTC is NEVER treated as
  bot-owned;
* the currently-active protective stop (persisted, not reconstructed
  from arbitrary historical orders);
* ``stopped_out_lockout`` — set on any protective fill, cleared ONLY on
  the next newly processed CASH transition while flat;
* the last reconciled ``available`` + ``locked`` venue balances so the
  next cycle can refuse unexplained KRW/BTC drift;
* the ``adopted_existing_btc`` flag — a one-shot record so a repeated
  ``--adopt-existing-btc`` on a later run cannot re-adopt.

Every mutation is a full-record rewrite through
:func:`bithumb_bot.artifact.canonical.write_with_sidecar`, so the state
file and its ``.sha256`` sidecar are always coherent on disk. A refused
cycle never writes — the on-disk bytes remain byte-identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast

from bithumb_bot.artifact.canonical import (
    canonical_bytes,
    sha256_hex,
    write_with_sidecar,
)
from bithumb_bot.bithumb_spec.snapshot import SnapshotV1, serialize_snapshot
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import BithumbBotError
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.strategy.breakout import (
    BREAKOUT_MARKET,
    BREAKOUT_UNIT_MINUTES,
    ENTRY_BUFFER_BPS,
    ENTRY_LOOKBACK_CANDLES,
    EXIT_LOOKBACK_CANDLES,
)

#: Bumped to 2 when :class:`ManagedFill` began persisting cumulative
#: ``paid_fee_krw`` (needed so buy KRW delta = ``-(executed_funds +
#: paid_fee)`` and sell KRW delta = ``+(executed_funds - paid_fee)``).
#: A v1 file on disk is refused at load time — the operator must clear
#: the old ``live_state.json``.
SCHEMA_VERSION: int = 2
STATE_FILENAME: str = "live_state.json"
STRATEGY_ID: str = "breakout_240m_v1"

#: 10% protective-stop fraction — mirrors the paper breakout runner. Kept
#: here so the persisted state pins the value it was written under.
PROTECTIVE_STOP_FRACTION: Decimal = Decimal("0.10")

_STRATEGY_CONSTANTS: dict[str, str] = {
    "entry_lookback_candles": str(ENTRY_LOOKBACK_CANDLES),
    "exit_lookback_candles": str(EXIT_LOOKBACK_CANDLES),
    "entry_buffer_bps": str(ENTRY_BUFFER_BPS),
    "protective_stop_fraction": str(PROTECTIVE_STOP_FRACTION),
}

StrategyState = Literal["CASH", "LONG"]

_STRATEGY_STATES: frozenset[str] = frozenset({"CASH", "LONG"})

_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "strategy_id",
        "strategy_constants",
        "market",
        "unit_minutes",
        "snapshot_sha256",
        "config_sha256",
        "processed_candles",
        "last_processed_open_time_utc",
        "strategy_state",
        "bot_owned_position_qty",
        "bot_owned_cost_basis_krw",
        "active_stop",
        "stopped_out_lockout",
        "last_reconciled_krw_available",
        "last_reconciled_krw_locked",
        "last_reconciled_btc_available",
        "last_reconciled_btc_locked",
        "known_managed_fills",
        "adopted_existing_btc",
    }
)

_ACTIVE_STOP_KEYS: frozenset[str] = frozenset(
    {"stop_price", "qty", "activated_at_utc", "entry_fill_price"}
)

_MANAGED_FILL_KEYS: frozenset[str] = frozenset(
    {"side", "filled_qty", "filled_notional_krw", "paid_fee_krw"}
)

_PROCESSED_CANDLE_KEYS: frozenset[str] = frozenset(
    {"open_time_utc", "fingerprint"}
)

_HEX64_CHARS: frozenset[str] = frozenset("0123456789abcdef")


class LiveStateError(BithumbBotError):
    """Base error for live-state validation."""


class LiveStateCorrupt(LiveStateError):
    """Persisted state file failed schema / integrity / fingerprint checks.

    Raised BEFORE any broker mutation. The on-disk file and its sidecar
    are NEVER modified as a side effect of the refusal — they remain
    byte-identical so an operator can inspect the exact bytes.
    """


class LiveStateDrift(LiveStateError):
    """Persisted state is intact but disagrees with the current world.

    Fires when snapshot/config sha, processed-candle prefix, or venue
    balance vs. previous-reconciled-plus-known-deltas does not add up.
    """


@dataclass(frozen=True)
class ProcessedCandle:
    """Fingerprinted record of one completed candle the strategy consumed."""

    open_time_utc: datetime
    fingerprint: str


@dataclass(frozen=True)
class ActiveStop:
    """Persisted active protective stop.

    Reconstructed byte-identically from disk. ``qty`` is the currently
    protected quantity and grows only as new managed buy fills settle.
    """

    stop_price: Money
    qty: Qty
    activated_at_utc: datetime
    entry_fill_price: Money


@dataclass(frozen=True)
class ManagedFill:
    """Cumulative fill totals observed for one managed order.

    ``paid_fee_krw`` is the venue's cumulative KRW fee for this order —
    persisted so the next cycle can compute the incremental KRW delta
    (``buy: -(new_notional + new_fee), sell: +(new_notional - new_fee)``)
    without re-estimating fees.
    """

    side: Literal["buy", "sell"]
    filled_qty: Qty
    filled_notional_krw: Money
    paid_fee_krw: Money


@dataclass(frozen=True)
class LiveState:
    """Authoritative persisted live-trading state."""

    schema_version: int
    strategy_id: str
    strategy_constants: dict[str, str]
    market: str
    unit_minutes: int
    snapshot_sha256: str
    config_sha256: str
    processed_candles: tuple[ProcessedCandle, ...]
    last_processed_open_time_utc: datetime | None
    strategy_state: StrategyState
    bot_owned_position_qty: Qty
    bot_owned_cost_basis_krw: Money
    active_stop: ActiveStop | None
    stopped_out_lockout: bool
    last_reconciled_krw_available: Money
    last_reconciled_krw_locked: Money
    last_reconciled_btc_available: Qty
    last_reconciled_btc_locked: Qty
    known_managed_fills: dict[str, ManagedFill]
    adopted_existing_btc: bool


# ---------------------------------------------------------------------------
# Deterministic hashing
# ---------------------------------------------------------------------------


def compute_candle_fingerprint(candle: Candle) -> str:
    """Return a stable hex fingerprint for one completed candle."""
    payload = {
        "market": candle.market,
        "unit_minutes": candle.unit_minutes,
        "open_time_utc": candle.open_time_utc.astimezone(UTC).isoformat(),
        "open": str(candle.open.value),
        "high": str(candle.high.value),
        "low": str(candle.low.value),
        "close": str(candle.close.value),
        "volume": str(candle.volume.value),
        "quote_volume": str(candle.quote_volume.value),
    }
    return sha256_hex(canonical_bytes(payload))


def compute_snapshot_sha256(snapshot: SnapshotV1) -> str:
    """Return the SHA-256 hex of the snapshot's canonical serialization."""
    return sha256_hex(serialize_snapshot(snapshot))


def compute_config_sha256(
    *, market: str, unit_minutes: int, max_notional_krw: Money
) -> str:
    """Return a SHA-256 hex covering mode-independent trading inputs."""
    payload = {
        "strategy_id": STRATEGY_ID,
        "strategy_constants": _STRATEGY_CONSTANTS,
        "market": market,
        "unit_minutes": unit_minutes,
        "max_notional_krw": str(max_notional_krw.value),
    }
    return sha256_hex(canonical_bytes(payload))


# ---------------------------------------------------------------------------
# Serialize / deserialize
# ---------------------------------------------------------------------------


def _encode_state(state: LiveState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "strategy_id": state.strategy_id,
        "strategy_constants": dict(state.strategy_constants),
        "market": state.market,
        "unit_minutes": state.unit_minutes,
        "snapshot_sha256": state.snapshot_sha256,
        "config_sha256": state.config_sha256,
        "processed_candles": [
            {
                "open_time_utc": p.open_time_utc.astimezone(UTC).isoformat(),
                "fingerprint": p.fingerprint,
            }
            for p in state.processed_candles
        ],
        "last_processed_open_time_utc": (
            state.last_processed_open_time_utc.astimezone(UTC).isoformat()
            if state.last_processed_open_time_utc is not None
            else None
        ),
        "strategy_state": state.strategy_state,
        "bot_owned_position_qty": str(state.bot_owned_position_qty.value),
        "bot_owned_cost_basis_krw": str(state.bot_owned_cost_basis_krw.value),
        "active_stop": (
            {
                "stop_price": str(state.active_stop.stop_price.value),
                "qty": str(state.active_stop.qty.value),
                "activated_at_utc": state.active_stop.activated_at_utc.astimezone(
                    UTC
                ).isoformat(),
                "entry_fill_price": str(state.active_stop.entry_fill_price.value),
            }
            if state.active_stop is not None
            else None
        ),
        "stopped_out_lockout": state.stopped_out_lockout,
        "last_reconciled_krw_available": str(
            state.last_reconciled_krw_available.value
        ),
        "last_reconciled_krw_locked": str(state.last_reconciled_krw_locked.value),
        "last_reconciled_btc_available": str(
            state.last_reconciled_btc_available.value
        ),
        "last_reconciled_btc_locked": str(state.last_reconciled_btc_locked.value),
        "known_managed_fills": {
            cid: {
                "side": mf.side,
                "filled_qty": str(mf.filled_qty.value),
                "filled_notional_krw": str(mf.filled_notional_krw.value),
                "paid_fee_krw": str(mf.paid_fee_krw.value),
            }
            for cid, mf in sorted(state.known_managed_fills.items())
        },
        "adopted_existing_btc": state.adopted_existing_btc,
    }


def _require_bool(value: Any, name: str, source: Path) -> bool:
    if not isinstance(value, bool):
        raise LiveStateCorrupt(f"{source}: {name} must be bool, got {type(value).__name__}")
    return value


def _require_exact_int(value: Any, name: str, source: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveStateCorrupt(
            f"{source}: {name} must be int (not bool), got {type(value).__name__}"
        )
    return value


def _require_str(value: Any, name: str, source: Path) -> str:
    if not isinstance(value, str):
        raise LiveStateCorrupt(
            f"{source}: {name} must be str, got {type(value).__name__}"
        )
    return value


def _require_hex64(value: Any, name: str, source: Path) -> str:
    s = _require_str(value, name, source)
    if len(s) != 64 or any(c not in _HEX64_CHARS for c in s):
        raise LiveStateCorrupt(f"{source}: {name} must be 64-char lowercase hex, got {s!r}")
    return s


def _require_decimal_str(value: Any, name: str, source: Path) -> Decimal:
    s = _require_str(value, name, source)
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise LiveStateCorrupt(f"{source}: {name} not decimal-parseable: {s!r}") from exc
    if not d.is_finite():
        raise LiveStateCorrupt(f"{source}: {name} must be finite, got {s!r}")
    return d


def _require_nonneg_decimal_str(value: Any, name: str, source: Path) -> Decimal:
    d = _require_decimal_str(value, name, source)
    if d < 0:
        raise LiveStateCorrupt(f"{source}: {name} must be >= 0, got {d}")
    return d


def _require_positive_decimal_str(value: Any, name: str, source: Path) -> Decimal:
    d = _require_decimal_str(value, name, source)
    if d <= 0:
        raise LiveStateCorrupt(f"{source}: {name} must be > 0, got {d}")
    return d


def _require_tz_aware_iso(value: Any, name: str, source: Path) -> datetime:
    s = _require_str(value, name, source)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise LiveStateCorrupt(f"{source}: {name} not ISO-parsable: {s!r}") from exc
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise LiveStateCorrupt(f"{source}: {name} must be tz-aware, got {s!r}")
    return dt


def _decode_active_stop(raw: Any, source: Path) -> ActiveStop | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LiveStateCorrupt(f"{source}: active_stop must be object or null")
    if set(raw.keys()) != _ACTIVE_STOP_KEYS:
        raise LiveStateCorrupt(
            f"{source}: active_stop keys mismatch, got {sorted(raw.keys())!r}"
        )
    return ActiveStop(
        stop_price=Money(_require_positive_decimal_str(raw["stop_price"], "active_stop.stop_price", source)),
        qty=Qty(_require_positive_decimal_str(raw["qty"], "active_stop.qty", source)),
        activated_at_utc=_require_tz_aware_iso(raw["activated_at_utc"], "active_stop.activated_at_utc", source),
        entry_fill_price=Money(_require_positive_decimal_str(raw["entry_fill_price"], "active_stop.entry_fill_price", source)),
    )


def _decode_managed_fills(raw: Any, source: Path) -> dict[str, ManagedFill]:
    if not isinstance(raw, dict):
        raise LiveStateCorrupt(f"{source}: known_managed_fills must be object")
    out: dict[str, ManagedFill] = {}
    for cid, entry in raw.items():
        if not isinstance(cid, str) or not cid:
            raise LiveStateCorrupt(
                f"{source}: known_managed_fills key must be non-empty str"
            )
        if not isinstance(entry, dict):
            raise LiveStateCorrupt(
                f"{source}: known_managed_fills[{cid!r}] must be object"
            )
        if set(entry.keys()) != _MANAGED_FILL_KEYS:
            raise LiveStateCorrupt(
                f"{source}: known_managed_fills[{cid!r}] keys mismatch, "
                f"got {sorted(entry.keys())!r}"
            )
        side = entry["side"]
        if side not in ("buy", "sell"):
            raise LiveStateCorrupt(
                f"{source}: known_managed_fills[{cid!r}].side must be buy|sell"
            )
        out[cid] = ManagedFill(
            side=side,
            filled_qty=Qty(
                _require_nonneg_decimal_str(entry["filled_qty"], f"known_managed_fills[{cid}].filled_qty", source)
            ),
            filled_notional_krw=Money(
                _require_nonneg_decimal_str(
                    entry["filled_notional_krw"],
                    f"known_managed_fills[{cid}].filled_notional_krw",
                    source,
                )
            ),
            paid_fee_krw=Money(
                _require_nonneg_decimal_str(
                    entry["paid_fee_krw"],
                    f"known_managed_fills[{cid}].paid_fee_krw",
                    source,
                )
            ),
        )
    return out


def _decode_processed(raw: Any, source: Path) -> tuple[ProcessedCandle, ...]:
    if not isinstance(raw, list):
        raise LiveStateCorrupt(f"{source}: processed_candles must be list")
    out: list[ProcessedCandle] = []
    prior_dt: datetime | None = None
    for entry in raw:
        if not isinstance(entry, dict):
            raise LiveStateCorrupt(f"{source}: processed_candles entry not object")
        if set(entry.keys()) != _PROCESSED_CANDLE_KEYS:
            raise LiveStateCorrupt(
                f"{source}: processed_candles entry keys mismatch, "
                f"got {sorted(entry.keys())!r}"
            )
        dt = _require_tz_aware_iso(
            entry["open_time_utc"], "processed_candles[].open_time_utc", source
        )
        fp = _require_hex64(entry["fingerprint"], "processed_candles[].fingerprint", source)
        if prior_dt is not None and dt <= prior_dt:
            raise LiveStateCorrupt(
                f"{source}: processed_candles open_time_utc must be strictly ascending"
            )
        out.append(ProcessedCandle(open_time_utc=dt, fingerprint=fp))
        prior_dt = dt
    return tuple(out)


def _decode_state(data: Any, source: Path) -> LiveState:
    if not isinstance(data, dict):
        raise LiveStateCorrupt(f"{source}: top-level payload must be object")
    keys = set(data.keys())
    if keys != _TOP_LEVEL_KEYS:
        missing = _TOP_LEVEL_KEYS - keys
        extra = keys - _TOP_LEVEL_KEYS
        raise LiveStateCorrupt(
            f"{source}: schema keys mismatch (missing={sorted(missing)!r}, "
            f"extra={sorted(extra)!r})"
        )
    sv = _require_exact_int(data["schema_version"], "schema_version", source)
    if sv != SCHEMA_VERSION:
        raise LiveStateCorrupt(
            f"{source}: unsupported schema_version={sv!r} (expected {SCHEMA_VERSION})"
        )
    strategy_id = _require_str(data["strategy_id"], "strategy_id", source)
    if strategy_id != STRATEGY_ID:
        raise LiveStateCorrupt(
            f"{source}: strategy_id must be {STRATEGY_ID!r}, got {strategy_id!r}"
        )
    raw_consts = data["strategy_constants"]
    if not isinstance(raw_consts, dict):
        raise LiveStateCorrupt(f"{source}: strategy_constants must be object")
    for k, v in raw_consts.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise LiveStateCorrupt(
                f"{source}: strategy_constants entries must be str→str, got {k!r}:{v!r}"
            )
    if raw_consts != _STRATEGY_CONSTANTS:
        raise LiveStateCorrupt(
            f"{source}: strategy_constants must equal {_STRATEGY_CONSTANTS!r}, got {raw_consts!r}"
        )
    market = _require_str(data["market"], "market", source)
    if market != BREAKOUT_MARKET:
        raise LiveStateCorrupt(
            f"{source}: market must be {BREAKOUT_MARKET!r}, got {market!r}"
        )
    unit = _require_exact_int(data["unit_minutes"], "unit_minutes", source)
    if unit != BREAKOUT_UNIT_MINUTES:
        raise LiveStateCorrupt(
            f"{source}: unit_minutes must be {BREAKOUT_UNIT_MINUTES}, got {unit}"
        )
    snapshot_sha = _require_hex64(data["snapshot_sha256"], "snapshot_sha256", source)
    config_sha = _require_hex64(data["config_sha256"], "config_sha256", source)
    processed = _decode_processed(data["processed_candles"], source)
    raw_last = data["last_processed_open_time_utc"]
    if raw_last is None:
        last_processed: datetime | None = None
    else:
        last_processed = _require_tz_aware_iso(
            raw_last, "last_processed_open_time_utc", source
        )
    if last_processed is None and processed:
        raise LiveStateCorrupt(
            f"{source}: last_processed_open_time_utc is null but processed_candles is non-empty"
        )
    if last_processed is not None:
        if not processed:
            raise LiveStateCorrupt(
                f"{source}: last_processed_open_time_utc set but processed_candles is empty"
            )
        if processed[-1].open_time_utc != last_processed:
            raise LiveStateCorrupt(
                f"{source}: last_processed_open_time_utc does not match last processed_candles entry"
            )
    strategy_state = _require_str(data["strategy_state"], "strategy_state", source)
    if strategy_state not in _STRATEGY_STATES:
        raise LiveStateCorrupt(
            f"{source}: strategy_state must be one of {sorted(_STRATEGY_STATES)!r}, got {strategy_state!r}"
        )
    pos_qty = _require_nonneg_decimal_str(
        data["bot_owned_position_qty"], "bot_owned_position_qty", source
    )
    cost_basis = _require_nonneg_decimal_str(
        data["bot_owned_cost_basis_krw"], "bot_owned_cost_basis_krw", source
    )
    if (pos_qty == 0) != (cost_basis == 0):
        raise LiveStateCorrupt(
            f"{source}: bot_owned_position_qty and bot_owned_cost_basis_krw must both be zero or both positive"
        )
    active_stop = _decode_active_stop(data["active_stop"], source)
    lockout = _require_bool(data["stopped_out_lockout"], "stopped_out_lockout", source)
    krw_avail = _require_nonneg_decimal_str(
        data["last_reconciled_krw_available"], "last_reconciled_krw_available", source
    )
    krw_locked = _require_nonneg_decimal_str(
        data["last_reconciled_krw_locked"], "last_reconciled_krw_locked", source
    )
    btc_avail = _require_nonneg_decimal_str(
        data["last_reconciled_btc_available"], "last_reconciled_btc_available", source
    )
    btc_locked = _require_nonneg_decimal_str(
        data["last_reconciled_btc_locked"], "last_reconciled_btc_locked", source
    )
    fills = _decode_managed_fills(data["known_managed_fills"], source)
    adopted = _require_bool(data["adopted_existing_btc"], "adopted_existing_btc", source)
    return LiveState(
        schema_version=sv,
        strategy_id=strategy_id,
        strategy_constants=dict(_STRATEGY_CONSTANTS),
        market=market,
        unit_minutes=unit,
        snapshot_sha256=snapshot_sha,
        config_sha256=config_sha,
        processed_candles=processed,
        last_processed_open_time_utc=last_processed,
        strategy_state=cast(StrategyState, strategy_state),
        bot_owned_position_qty=Qty(pos_qty),
        bot_owned_cost_basis_krw=Money(cost_basis),
        active_stop=active_stop,
        stopped_out_lockout=lockout,
        last_reconciled_krw_available=Money(krw_avail),
        last_reconciled_krw_locked=Money(krw_locked),
        last_reconciled_btc_available=Qty(btc_avail),
        last_reconciled_btc_locked=Qty(btc_locked),
        known_managed_fills=fills,
        adopted_existing_btc=adopted,
    )


# ---------------------------------------------------------------------------
# Load / write
# ---------------------------------------------------------------------------


def state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILENAME


def sidecar_path(state_dir: Path) -> Path:
    return state_path(state_dir).with_name(STATE_FILENAME + ".sha256")


def load_state(state_dir: Path) -> LiveState | None:
    """Load and strictly validate the persisted state, or return ``None``.

    Refuses (raises :class:`LiveStateCorrupt`) if the sidecar is missing
    or does not match the on-disk bytes, if the JSON is non-canonical,
    or if any schema/type/value constraint fails. Never mutates the
    on-disk bytes.
    """
    target = state_path(state_dir)
    sidecar = sidecar_path(state_dir)
    target_exists = target.is_file()
    sidecar_exists = sidecar.is_file()
    if not target_exists:
        if sidecar_exists:
            raise LiveStateCorrupt(
                f"orphan sidecar without state file: {sidecar}"
            )
        return None
    if not sidecar_exists:
        raise LiveStateCorrupt(f"missing sidecar for state file {target}")
    raw = target.read_bytes()
    on_disk_hex = sha256_hex(raw)
    expected_sidecar = f"{on_disk_hex}  {target.name}\n".encode("utf-8")
    if sidecar.read_bytes() != expected_sidecar:
        raise LiveStateCorrupt(
            f"sidecar bytes must be exactly '<hex>  {target.name}\\n' with "
            f"hash matching JSON"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LiveStateCorrupt(f"{target}: invalid JSON: {exc}") from exc
    if canonical_bytes(data) != raw:
        raise LiveStateCorrupt(
            f"{target}: bytes are not canonical JSON (sort_keys, compact, trailing newline)"
        )
    return _decode_state(data, target)


def write_state(state_dir: Path, state: LiveState) -> None:
    """Serialize + atomically write ``state`` and its ``.sha256`` sidecar."""
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(_encode_state(state))
    write_with_sidecar(state_path(state_dir), payload)


# ---------------------------------------------------------------------------
# Prefix + fingerprint verification
# ---------------------------------------------------------------------------


def verify_processed_prefix(
    state: LiveState, dataset_candles: tuple[Candle, ...]
) -> None:
    """Verify the persisted processed-candle prefix still matches the dataset.

    Refuses if any candle in the persisted prefix is missing from the
    current dataset, mutated, reordered, or shortened. Never treats an
    on-disk state whose processed candles have "disappeared" from the
    dataset as trustworthy — the operator MUST reconcile.
    """
    by_open: dict[datetime, Candle] = {c.open_time_utc: c for c in dataset_candles}
    for entry in state.processed_candles:
        candle = by_open.get(entry.open_time_utc)
        if candle is None:
            raise LiveStateDrift(
                f"processed candle at {entry.open_time_utc.isoformat()} is missing "
                f"from current dataset — refuses fail-closed"
            )
        actual = compute_candle_fingerprint(candle)
        if actual != entry.fingerprint:
            raise LiveStateDrift(
                f"processed candle at {entry.open_time_utc.isoformat()} fingerprint "
                f"mismatch (persisted={entry.fingerprint}, current={actual}) — "
                f"refuses fail-closed"
            )


def append_processed(
    state: LiveState, candles: list[Candle]
) -> LiveState:
    """Return a new state with additional processed candles appended.

    Refuses if any candle is not strictly newer than the last recorded
    processed candle.
    """
    new_entries = list(state.processed_candles)
    prior = state.last_processed_open_time_utc
    for candle in candles:
        if prior is not None and candle.open_time_utc <= prior:
            raise LiveStateDrift(
                f"cannot append candle at {candle.open_time_utc.isoformat()} — "
                f"not strictly newer than last processed {prior.isoformat()}"
            )
        new_entries.append(
            ProcessedCandle(
                open_time_utc=candle.open_time_utc,
                fingerprint=compute_candle_fingerprint(candle),
            )
        )
        prior = candle.open_time_utc
    return replace(
        state,
        processed_candles=tuple(new_entries),
        last_processed_open_time_utc=prior,
    )


# ---------------------------------------------------------------------------
# Fresh-state construction
# ---------------------------------------------------------------------------


def initial_state(
    *,
    market: str,
    unit_minutes: int,
    snapshot_sha256: str,
    config_sha256: str,
) -> LiveState:
    """Return a zeroed state for a brand-new live-trading directory."""
    return LiveState(
        schema_version=SCHEMA_VERSION,
        strategy_id=STRATEGY_ID,
        strategy_constants=dict(_STRATEGY_CONSTANTS),
        market=market,
        unit_minutes=unit_minutes,
        snapshot_sha256=snapshot_sha256,
        config_sha256=config_sha256,
        processed_candles=(),
        last_processed_open_time_utc=None,
        strategy_state="CASH",
        bot_owned_position_qty=Qty(Decimal("0")),
        bot_owned_cost_basis_krw=Money(Decimal("0")),
        active_stop=None,
        stopped_out_lockout=False,
        last_reconciled_krw_available=Money(Decimal("0")),
        last_reconciled_krw_locked=Money(Decimal("0")),
        last_reconciled_btc_available=Qty(Decimal("0")),
        last_reconciled_btc_locked=Qty(Decimal("0")),
        known_managed_fills={},
        adopted_existing_btc=False,
    )


__all__ = [
    "PROTECTIVE_STOP_FRACTION",
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "STRATEGY_ID",
    "ActiveStop",
    "LiveState",
    "LiveStateCorrupt",
    "LiveStateDrift",
    "LiveStateError",
    "ManagedFill",
    "ProcessedCandle",
    "StrategyState",
    "append_processed",
    "compute_candle_fingerprint",
    "compute_config_sha256",
    "compute_snapshot_sha256",
    "initial_state",
    "load_state",
    "sidecar_path",
    "state_path",
    "verify_processed_prefix",
    "write_state",
]
