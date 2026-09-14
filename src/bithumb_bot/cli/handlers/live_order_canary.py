"""`bt live order-canary` handler — restart-safe live-order canary.

Verifies the real Bithumb market-buy and market-sell path against a
hard-capped 10,000 KRW notional BEFORE any strategy capital is put at
risk. This is the ONE code path allowed to operate before
``live_order_acceptance`` is confirmed on the snapshot; the regular
``bt live breakout-cycle`` readiness gate is NOT weakened by this.

Safety envelope:

* Requires ALL of ``--mode live``, ``--enable-live-orders``, and
  ``--confirm-canary``. Missing any → refuse.
* Market-buy notional is hard-capped at ``10000`` KRW in code. There
  is no CLI flag to raise it.
* Uses :class:`LiveBithumbBroker` and its existing persistent
  managed-order mapping. Withdrawal permission, credential, and
  endpoint remain nonexistent (D-69).
* Persists a separate canonical canary state file
  (``state_dir/canary_state.json``) plus SHA-256 sidecar, distinct
  from the breakout-cycle ``state.json``.
* State machine (persisted): ``INIT → BUY_SUBMITTED → BUY_FILLED →
  SELL_SUBMITTED → COMPLETE``. Each invocation submits AT MOST one
  new order. Restart reconciles via the broker's idempotent
  ``submit`` (deterministic client_order_id derived from the persisted
  ``canary_init_utc``); no POST is ever repeated.
* Sells only the exact BTC quantity acquired by the canary buy. Never
  touches unrelated BTC.
* Partial, ambiguous, rejected, canceled, malformed, and balance-drift
  cases halt cleanly (exit non-zero, no state advancement).
* Supports a ``state_dir/HALT`` file — when present, reconciliation
  still runs but no new order is submitted.
* On ``COMPLETE`` writes a canonical machine-readable
  ``canary_report.json`` + sidecar containing ids, fills, paid fees,
  timestamps, venue balances, and verification outcomes.
* Never logs credentials, JWTs, headers, or raw private responses —
  only broker-derived integers, decimal-strings, timestamps, and
  sanitized error names.
* Does NOT touch the snapshot's ``live_order_acceptance`` field; the
  report is evidence for later independent approval.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal


# Hardcoded notional cap. Operator cannot raise it — there is no CLI
# flag; changing it is a code-level modification.
_HARD_CAP_BUY_KRW: Decimal = Decimal("10000")
_UNIT_MINUTES: int = 1
_STATE_FILE_NAME = "canary_state.json"
_REPORT_FILE_NAME = "canary_report.json"
_STATE_SCHEMA_VERSION = 1
_REPORT_SCHEMA_VERSION = 1

_Phase = Literal[
    "INIT", "BUY_SUBMITTED", "BUY_FILLED", "SELL_SUBMITTED", "COMPLETE"
]

_ALLOWED_PHASES: frozenset[str] = frozenset(
    {"INIT", "BUY_SUBMITTED", "BUY_FILLED", "SELL_SUBMITTED", "COMPLETE"}
)


@dataclass(frozen=True)
class _CanaryState:
    """Persisted canary state — deterministic + hash-verifiable."""

    phase: _Phase
    canary_init_utc: str  # ISO-8601 UTC, tz-aware
    buy_amount_krw: str  # Decimal string, always "10000"
    buy_client_order_id: str | None  # 64-hex, once buy intent is built
    buy_filled_qty: str  # Decimal string, "0" until buy fills
    buy_filled_notional_krw: str
    buy_paid_fee_krw: str
    buy_state: str | None  # broker OrderState.value or None
    sell_client_order_id: str | None
    sell_filled_qty: str
    sell_filled_notional_krw: str
    sell_paid_fee_krw: str
    sell_state: str | None
    initial_balances: dict[str, str] | None = None
    post_buy_balances: dict[str, str] | None = None
    post_sell_balances: dict[str, str] | None = None
    events: tuple[dict[str, str], ...] = field(default_factory=tuple)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def handler(args: argparse.Namespace) -> int:  # noqa: C901 — linear state machine
    """Entry point for ``bt live order-canary``."""
    from bithumb_bot.artifact.canonical import (
        canonical_bytes,
        sha256_hex,
        write_with_sidecar,
    )
    from bithumb_bot.artifact.timestamps import utc_now
    from bithumb_bot.config.validator import REPO_ROOT_ENV, validate

    _result = validate(("live", "order-canary"))
    if not _result.ok:
        print(
            f"bt live order-canary: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    mode = getattr(args, "mode", None)
    enable_live = bool(getattr(args, "enable_live_orders", False))
    confirm = bool(getattr(args, "confirm_canary", False))
    state_dir = Path(args.state_dir)

    if mode != "live":
        print(
            "bt live order-canary: --mode live is required (dry-run has no canary)",
            file=sys.stderr,
        )
        return 1
    if not enable_live:
        print(
            "bt live order-canary: --enable-live-orders is required",
            file=sys.stderr,
        )
        return 1
    if not confirm:
        print(
            "bt live order-canary: --confirm-canary is required",
            file=sys.stderr,
        )
        return 1

    from bithumb_bot.broker.live import (
        AmbiguousSubmitError,
        LiveBithumbBroker,
        LiveBrokerError,
        VenueHTTPError,
        VenueResponseError,
    )
    from bithumb_bot.broker.state import OrderState
    from bithumb_bot.core.money import Money, Qty
    from bithumb_bot.execution.intent import OrderIntent
    from bithumb_bot.secrets.live import (
        TradeCredentialsMissingError,
        load_trade_credentials,
    )

    repo_root = Path(os.environ.get(REPO_ROOT_ENV) or Path.cwd())
    try:
        creds = load_trade_credentials(repo_root)
    except TradeCredentialsMissingError as exc:
        print(f"bt live order-canary: {exc}", file=sys.stderr)
        return 1

    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / _STATE_FILE_NAME
    report_path = state_dir / _REPORT_FILE_NAME
    halt_file = state_dir / "HALT"

    try:
        existing = _load_state(state_path)
    except _CanaryStateCorrupt as exc:
        print(f"bt live order-canary: {exc}", file=sys.stderr)
        return 1

    now_utc = utc_now()

    live_broker = LiveBithumbBroker(
        access_key=creds.access_key,
        secret_key=creds.secret_key,
        state_dir=state_dir,
    )
    del creds

    def _persist(state: _CanaryState) -> None:
        payload = canonical_bytes(_encode_state(state))
        write_with_sidecar(state_path, payload)

    def _record_event(state: _CanaryState, phase: _Phase, note: str) -> _CanaryState:
        return _replace(
            state,
            events=state.events + (
                {"at_utc": now_utc.isoformat(), "phase": phase, "note": note},
            ),
        )

    def _snapshot_balances() -> dict[str, str]:
        b = live_broker.fetch_balances()
        return {
            "cash_krw": str(b.cash_krw.value),
            "cash_krw_locked": str(b.cash_krw_locked.value),
            "coin_qty": str(b.coin_qty.value),
            "coin_qty_locked": str(b.coin_qty_locked.value),
        }

    # -- Case: already COMPLETE ------------------------------------
    if existing is not None and existing.phase == "COMPLETE":
        print(f"canary already COMPLETE — report: {report_path}")
        return 0

    # -- HALT file: reconcile but do not submit --------------------
    if halt_file.exists():
        try:
            live_broker.reconcile_all()
        except (LiveBrokerError, VenueHTTPError, VenueResponseError) as exc:
            print(
                f"bt live order-canary: HALT reconcile refused "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            return 1
        current_phase = existing.phase if existing is not None else "INIT"
        print(f"HALT present — reconciled without submitting; phase={current_phase}")
        if existing is not None:
            _persist(_record_event(existing, current_phase, "halt-reconcile"))
        return 0

    # -- INIT -----------------------------------------------------
    if existing is None:
        init_utc = now_utc.replace(microsecond=0)
        buy_intent = _build_buy_intent(init_utc)
        from bithumb_bot.broker.identity import deterministic_client_order_id

        buy_cid = deterministic_client_order_id(buy_intent)
        initial_balances = _try_fetch_balances(live_broker, "initial balances")
        state = _CanaryState(
            phase="INIT",
            canary_init_utc=init_utc.isoformat(),
            buy_amount_krw=str(_HARD_CAP_BUY_KRW),
            buy_client_order_id=buy_cid,
            buy_filled_qty="0",
            buy_filled_notional_krw="0",
            buy_paid_fee_krw="0",
            buy_state=None,
            sell_client_order_id=None,
            sell_filled_qty="0",
            sell_filled_notional_krw="0",
            sell_paid_fee_krw="0",
            sell_state=None,
            initial_balances=initial_balances,
        )
        state = _record_event(state, "INIT", "canary initialized")
        _persist(state)
        existing = state

    state = existing

    # -- INIT → BUY_SUBMITTED / BUY_FILLED -------------------------
    if state.phase == "INIT":
        init_utc = _parse_iso(state.canary_init_utc, "canary_init_utc")
        buy_intent = _build_buy_intent(init_utc)
        try:
            buy_order = live_broker.submit(buy_intent)
        except (
            AmbiguousSubmitError,
            LiveBrokerError,
            VenueHTTPError,
            VenueResponseError,
        ) as exc:
            print(
                f"bt live order-canary: buy submit halted "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            return 1
        state = _apply_buy_order(state, buy_order)
        if state.phase == "BUY_FILLED":
            state = _replace(
                state, post_buy_balances=_try_fetch_balances(live_broker, "post-buy"),
            )
        state = _record_event(state, state.phase, "buy submit result")
        _persist(state)
        _halt_on_bad(state, "buy")
        print(
            f"phase={state.phase} buy_state={state.buy_state} "
            f"buy_client_order_id={state.buy_client_order_id[:12] if state.buy_client_order_id else '-'}"
        )
        return 0

    # -- BUY_SUBMITTED: reconcile only ------------------------------
    if state.phase == "BUY_SUBMITTED":
        init_utc = _parse_iso(state.canary_init_utc, "canary_init_utc")
        buy_intent = _build_buy_intent(init_utc)
        try:
            buy_order = live_broker.submit(buy_intent)
        except (
            AmbiguousSubmitError,
            LiveBrokerError,
            VenueHTTPError,
            VenueResponseError,
        ) as exc:
            print(
                f"bt live order-canary: buy reconcile halted "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            return 1
        state = _apply_buy_order(state, buy_order)
        if state.phase == "BUY_FILLED" and state.post_buy_balances is None:
            state = _replace(
                state,
                post_buy_balances=_try_fetch_balances(live_broker, "post-buy"),
            )
        state = _record_event(state, state.phase, "buy reconcile")
        _persist(state)
        _halt_on_bad(state, "buy")
        print(f"phase={state.phase} buy_state={state.buy_state}")
        if state.phase == "BUY_SUBMITTED":
            return 0

    # -- BUY_FILLED → SELL_SUBMITTED / straight to COMPLETE --------
    if state.phase == "BUY_FILLED":
        buy_qty = _parse_decimal(state.buy_filled_qty, "buy_filled_qty")
        if buy_qty <= 0:
            print(
                "bt live order-canary: BUY_FILLED with non-positive filled_qty "
                "— halting for operator review",
                file=sys.stderr,
            )
            return 1
        # Balance sanity: venue coin_qty (available + locked) must be at
        # least the qty we're about to sell (drift check).
        try:
            balances = live_broker.fetch_balances()
        except (LiveBrokerError, VenueHTTPError, VenueResponseError) as exc:
            print(
                f"bt live order-canary: balance fetch halted "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            return 1
        held = balances.coin_qty.value + balances.coin_qty_locked.value
        if held < buy_qty:
            print(
                f"bt live order-canary: balance drift — venue BTC total {held} "
                f"< canary buy_filled_qty {buy_qty}; halt",
                file=sys.stderr,
            )
            return 1
        init_utc = _parse_iso(state.canary_init_utc, "canary_init_utc")
        sell_intent = _build_sell_intent(init_utc, Qty(buy_qty))
        from bithumb_bot.broker.identity import deterministic_client_order_id

        sell_cid = deterministic_client_order_id(sell_intent)
        state = _replace(state, sell_client_order_id=sell_cid)
        _persist(_record_event(state, "BUY_FILLED", "sell intent built"))
        try:
            sell_order = live_broker.submit(sell_intent)
        except (
            AmbiguousSubmitError,
            LiveBrokerError,
            VenueHTTPError,
            VenueResponseError,
        ) as exc:
            print(
                f"bt live order-canary: sell submit halted "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            return 1
        state = _apply_sell_order(state, sell_order, buy_qty)
        if state.phase == "COMPLETE":
            state = _replace(
                state,
                post_sell_balances=_try_fetch_balances(live_broker, "post-sell"),
            )
        state = _record_event(state, state.phase, "sell submit result")
        _persist(state)
        _halt_on_bad(state, "sell")
        if state.phase == "COMPLETE":
            _write_report(report_path, state)
            print(f"phase=COMPLETE report: {report_path}")
            return 0
        print(f"phase={state.phase} sell_state={state.sell_state}")
        return 0

    # -- SELL_SUBMITTED: reconcile only -----------------------------
    if state.phase == "SELL_SUBMITTED":
        init_utc = _parse_iso(state.canary_init_utc, "canary_init_utc")
        buy_qty = _parse_decimal(state.buy_filled_qty, "buy_filled_qty")
        sell_intent = _build_sell_intent(init_utc, Qty(buy_qty))
        try:
            sell_order = live_broker.submit(sell_intent)
        except (
            AmbiguousSubmitError,
            LiveBrokerError,
            VenueHTTPError,
            VenueResponseError,
        ) as exc:
            print(
                f"bt live order-canary: sell reconcile halted "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            return 1
        state = _apply_sell_order(state, sell_order, buy_qty)
        if state.phase == "COMPLETE" and state.post_sell_balances is None:
            state = _replace(
                state,
                post_sell_balances=_try_fetch_balances(live_broker, "post-sell"),
            )
        state = _record_event(state, state.phase, "sell reconcile")
        _persist(state)
        _halt_on_bad(state, "sell")
        if state.phase == "COMPLETE":
            _write_report(report_path, state)
            print(f"phase=COMPLETE report: {report_path}")
            return 0
        print(f"phase={state.phase} sell_state={state.sell_state}")
        return 0

    # Should not reach here — every phase has an explicit branch above.
    print(
        f"bt live order-canary: unhandled phase {state.phase!r}",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# Intent builders
# ---------------------------------------------------------------------------


def _build_buy_intent(canary_init_utc: datetime) -> "OrderIntent":
    from datetime import timedelta

    from bithumb_bot.core.money import Money
    from bithumb_bot.execution.intent import OrderIntent

    return OrderIntent(
        side="buy",
        source_open_time_utc=canary_init_utc,
        unit_minutes=_UNIT_MINUTES,
        signal_ts_utc=canary_init_utc + timedelta(minutes=_UNIT_MINUTES),
        requested_notional_krw=Money(_HARD_CAP_BUY_KRW),
        requested_qty=None,
    )


def _build_sell_intent(canary_init_utc: datetime, qty: "Qty") -> "OrderIntent":  # type: ignore[name-defined]
    from datetime import timedelta

    from bithumb_bot.execution.intent import OrderIntent

    return OrderIntent(
        side="sell",
        source_open_time_utc=canary_init_utc,
        unit_minutes=_UNIT_MINUTES,
        signal_ts_utc=canary_init_utc + timedelta(minutes=_UNIT_MINUTES),
        requested_notional_krw=None,
        requested_qty=qty,
    )


# ---------------------------------------------------------------------------
# Order-result application
# ---------------------------------------------------------------------------


def _apply_buy_order(state: _CanaryState, order: Any) -> _CanaryState:
    from bithumb_bot.broker.state import OrderState

    filled_qty = str(order.filled_qty.value)
    filled_notional = str(order.filled_notional_krw.value)
    paid_fee = str(order.paid_fee_krw.value)
    order_state = order.state.value
    phase: _Phase = state.phase
    if order.state is OrderState.FILLED:
        phase = "BUY_FILLED"
    elif order.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
        phase = "BUY_SUBMITTED"
    else:
        # REJECTED / CANCELED — remain in current phase but record state
        # so the halt-guard trips.
        phase = state.phase if state.phase != "INIT" else "BUY_SUBMITTED"
    return _replace(
        state,
        phase=phase,
        buy_filled_qty=filled_qty,
        buy_filled_notional_krw=filled_notional,
        buy_paid_fee_krw=paid_fee,
        buy_state=order_state,
    )


def _apply_sell_order(
    state: _CanaryState, order: Any, buy_qty: Decimal
) -> _CanaryState:
    from bithumb_bot.broker.state import OrderState

    filled_qty = order.filled_qty.value
    filled_notional = order.filled_notional_krw.value
    paid_fee = order.paid_fee_krw.value
    order_state = order.state.value
    phase: _Phase = state.phase
    if order.state is OrderState.FILLED:
        if filled_qty != buy_qty:
            # A sell that fills a different qty than we requested is
            # ambiguous. Do NOT advance to COMPLETE; halt.
            phase = state.phase
        else:
            phase = "COMPLETE"
    elif order.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
        phase = "SELL_SUBMITTED"
    # else: REJECTED / CANCELED — remain in current phase; halt-guard trips.
    return _replace(
        state,
        phase=phase,
        sell_filled_qty=str(filled_qty),
        sell_filled_notional_krw=str(filled_notional),
        sell_paid_fee_krw=str(paid_fee),
        sell_state=order_state,
    )


def _halt_on_bad(state: _CanaryState, leg: str) -> None:
    """Halt the process (sys.exit 1) if the just-observed leg is unrecoverable.

    Terminal-bad = REJECTED or CANCELED. Long-lived PARTIAL leaves the
    phase at BUY_SUBMITTED / SELL_SUBMITTED which is a NORMAL waiting
    state — not a halt. The caller decides whether to retry later.
    """
    if leg == "buy":
        st = state.buy_state
    else:
        st = state.sell_state
    if st in ("rejected", "canceled"):
        print(
            f"bt live order-canary: {leg} terminated with state={st!r}; "
            "halting for operator review",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class _CanaryStateCorrupt(Exception):
    """Raised when the persisted canary state file fails integrity checks."""


def _encode_state(state: _CanaryState) -> dict[str, Any]:
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "phase": state.phase,
        "canary_init_utc": state.canary_init_utc,
        "buy_amount_krw": state.buy_amount_krw,
        "buy_client_order_id": state.buy_client_order_id,
        "buy_filled_qty": state.buy_filled_qty,
        "buy_filled_notional_krw": state.buy_filled_notional_krw,
        "buy_paid_fee_krw": state.buy_paid_fee_krw,
        "buy_state": state.buy_state,
        "sell_client_order_id": state.sell_client_order_id,
        "sell_filled_qty": state.sell_filled_qty,
        "sell_filled_notional_krw": state.sell_filled_notional_krw,
        "sell_paid_fee_krw": state.sell_paid_fee_krw,
        "sell_state": state.sell_state,
        "initial_balances": state.initial_balances,
        "post_buy_balances": state.post_buy_balances,
        "post_sell_balances": state.post_sell_balances,
        "events": list(state.events),
    }


def _load_state(state_path: Path) -> _CanaryState | None:
    from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex

    if not state_path.is_file():
        return None
    raw = state_path.read_bytes()
    sidecar = state_path.with_name(state_path.name + ".sha256")
    if not sidecar.is_file():
        raise _CanaryStateCorrupt(f"{state_path}: sidecar missing")
    recorded = sidecar.read_text(encoding="utf-8").strip().split()
    if not recorded or len(recorded[0]) != 64 or recorded[0] != sha256_hex(raw):
        raise _CanaryStateCorrupt(f"{state_path}: sidecar hash mismatch")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _CanaryStateCorrupt(f"{state_path}: invalid JSON: {exc}") from exc
    if canonical_bytes(data) != raw:
        raise _CanaryStateCorrupt(f"{state_path}: bytes not canonical")
    if not isinstance(data, dict):
        raise _CanaryStateCorrupt(f"{state_path}: root not object")
    sv = data.get("schema_version")
    if sv != _STATE_SCHEMA_VERSION:
        raise _CanaryStateCorrupt(
            f"{state_path}: unsupported schema_version={sv!r}"
        )
    phase = data.get("phase")
    if phase not in _ALLOWED_PHASES:
        raise _CanaryStateCorrupt(f"{state_path}: bad phase {phase!r}")
    canary_init = data.get("canary_init_utc")
    if not isinstance(canary_init, str) or not canary_init:
        raise _CanaryStateCorrupt(f"{state_path}: canary_init_utc invalid")
    buy_amt = data.get("buy_amount_krw")
    if buy_amt != str(_HARD_CAP_BUY_KRW):
        raise _CanaryStateCorrupt(
            f"{state_path}: buy_amount_krw={buy_amt!r} does not match "
            f"hardcoded cap {str(_HARD_CAP_BUY_KRW)!r}"
        )
    buy_cid = data.get("buy_client_order_id")
    if buy_cid is not None and (
        not isinstance(buy_cid, str) or not _HEX64.fullmatch(buy_cid)
    ):
        raise _CanaryStateCorrupt(f"{state_path}: buy_client_order_id malformed")
    sell_cid = data.get("sell_client_order_id")
    if sell_cid is not None and (
        not isinstance(sell_cid, str) or not _HEX64.fullmatch(sell_cid)
    ):
        raise _CanaryStateCorrupt(f"{state_path}: sell_client_order_id malformed")
    for key in (
        "buy_filled_qty",
        "buy_filled_notional_krw",
        "buy_paid_fee_krw",
        "sell_filled_qty",
        "sell_filled_notional_krw",
        "sell_paid_fee_krw",
    ):
        v = data.get(key)
        if not isinstance(v, str):
            raise _CanaryStateCorrupt(f"{state_path}: {key} must be string")
        try:
            d = Decimal(v)
        except InvalidOperation as exc:
            raise _CanaryStateCorrupt(f"{state_path}: {key} not decimal") from exc
        if not d.is_finite() or d < 0:
            raise _CanaryStateCorrupt(f"{state_path}: {key} not finite nonneg")
    events_raw = data.get("events") or []
    if not isinstance(events_raw, list):
        raise _CanaryStateCorrupt(f"{state_path}: events must be list")
    events: list[dict[str, str]] = []
    for entry in events_raw:
        if not isinstance(entry, dict):
            raise _CanaryStateCorrupt(f"{state_path}: event entry not object")
        events.append({str(k): str(v) for k, v in entry.items()})
    return _CanaryState(
        phase=phase,
        canary_init_utc=canary_init,
        buy_amount_krw=buy_amt,
        buy_client_order_id=buy_cid,
        buy_filled_qty=data["buy_filled_qty"],
        buy_filled_notional_krw=data["buy_filled_notional_krw"],
        buy_paid_fee_krw=data["buy_paid_fee_krw"],
        buy_state=data.get("buy_state"),
        sell_client_order_id=sell_cid,
        sell_filled_qty=data["sell_filled_qty"],
        sell_filled_notional_krw=data["sell_filled_notional_krw"],
        sell_paid_fee_krw=data["sell_paid_fee_krw"],
        sell_state=data.get("sell_state"),
        initial_balances=_coerce_balances(data.get("initial_balances")),
        post_buy_balances=_coerce_balances(data.get("post_buy_balances")),
        post_sell_balances=_coerce_balances(data.get("post_sell_balances")),
        events=tuple(events),
    )


def _coerce_balances(v: Any) -> dict[str, str] | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise _CanaryStateCorrupt("balances snapshot must be object or null")
    return {str(k): str(val) for k, val in v.items()}


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def _write_report(report_path: Path, state: _CanaryState) -> None:
    from bithumb_bot.artifact.canonical import canonical_bytes, write_with_sidecar

    verification = {
        "buy_filled_fully": state.buy_state == "filled",
        "sell_filled_fully": state.sell_state == "filled",
        "sell_qty_matches_buy_qty": (
            Decimal(state.sell_filled_qty) == Decimal(state.buy_filled_qty)
        ),
        "buy_amount_cap_respected": state.buy_amount_krw == str(_HARD_CAP_BUY_KRW),
    }
    payload = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "status": "COMPLETE",
        "canary_init_utc": state.canary_init_utc,
        "buy": {
            "client_order_id": state.buy_client_order_id,
            "requested_notional_krw": state.buy_amount_krw,
            "filled_qty": state.buy_filled_qty,
            "filled_notional_krw": state.buy_filled_notional_krw,
            "paid_fee_krw": state.buy_paid_fee_krw,
            "state": state.buy_state,
        },
        "sell": {
            "client_order_id": state.sell_client_order_id,
            "requested_qty": state.buy_filled_qty,
            "filled_qty": state.sell_filled_qty,
            "filled_notional_krw": state.sell_filled_notional_krw,
            "paid_fee_krw": state.sell_paid_fee_krw,
            "state": state.sell_state,
        },
        "balances": {
            "initial": state.initial_balances,
            "post_buy": state.post_buy_balances,
            "post_sell": state.post_sell_balances,
        },
        "verification_outcomes": verification,
        "events": list(state.events),
    }
    write_with_sidecar(report_path, canonical_bytes(payload))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _try_fetch_balances(broker: Any, label: str) -> dict[str, str] | None:
    try:
        b = broker.fetch_balances()
    except Exception:  # noqa: BLE001 — balances are evidence, not correctness
        return None
    return {
        "cash_krw": str(b.cash_krw.value),
        "cash_krw_locked": str(b.cash_krw_locked.value),
        "coin_qty": str(b.coin_qty.value),
        "coin_qty_locked": str(b.coin_qty_locked.value),
    }


def _parse_iso(raw: str, field_name: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _parse_decimal(raw: str, field_name: str) -> Decimal:
    d = Decimal(raw)
    if not d.is_finite():
        raise ValueError(f"{field_name} not finite")
    return d


def _replace(state: _CanaryState, **changes: Any) -> _CanaryState:
    from dataclasses import replace

    return replace(state, **changes)


__all__ = ["handler"]
