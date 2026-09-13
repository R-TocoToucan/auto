"""Connect one ``BreakoutSignal`` to the venue-agnostic Broker interface.

Bounded translator:

* LONG signal in CASH (and not stopped-out) becomes a full-sleeve
  fee-aware buy ``OrderIntent``.
* CASH signal while holding coin becomes a full-position sell
  ``OrderIntent``.
* Every other combination is a no-op (returns ``None``).
* A ``BreakoutSignal`` observed strictly before its own close boundary
  is refused fail-closed; equality is honored because the completed
  candle close IS the next candle's open boundary.
* Before submission, any pre-existing unresolved order that is not the
  same intent as the one about to be submitted also fails closed. The
  same intent may be retried idempotently — ``Broker.submit`` is
  idempotent by contract.

This module intentionally does no strategy math (that lives in
``strategy/``) and no engine-side accounting (that lives in
``execution/``). It never calls MockBroker-specific mutation methods
(``record_fill`` / ``cancel`` / ``reject``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from bithumb_bot.broker.interface import Broker
from bithumb_bot.broker.state import BrokerOrder
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent
from bithumb_bot.strategy.breakout import BREAKOUT_UNIT_MINUTES, BreakoutSignal


class DispatchRefused(Exception):
    """Refuse to dispatch this signal. No broker mutation attempted.

    Raised fail-closed for any of:

    * An order-boundary input fails validation (unknown ``target_state``,
      naive datetime, wrong ``unit_minutes``, inconsistent
      ``signal_ts_utc``, bad fee, non-finite/negative portfolio value,
      non-``bool`` lockout, non-positive cap).
    * ``observed_at_utc`` precedes ``signal.signal_ts_utc``.
    * The full-sleeve pre-fee LONG notional exceeds ``max_notional_krw``.
    * The broker holds an unresolved order that is not this same intent.
    """


def _is_tz_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def _validate_inputs(
    *,
    signal: BreakoutSignal,
    cash: Money,
    position: Qty,
    bid_fee_rate: Decimal,
    max_notional_krw: Money,
    stopped_out_lockout: bool,
    observed_at_utc: datetime,
) -> None:
    if signal.target_state not in ("LONG", "CASH"):
        raise DispatchRefused(
            f"signal.target_state must be exactly 'LONG' or 'CASH', got "
            f"{signal.target_state!r}"
        )
    for name, dt in (
        ("observed_at_utc", observed_at_utc),
        ("signal.source_open_time_utc", signal.source_open_time_utc),
        ("signal.signal_ts_utc", signal.signal_ts_utc),
    ):
        if not _is_tz_aware(dt):
            raise DispatchRefused(
                f"{name} must be timezone-aware, got {dt.isoformat()!r}"
            )
    if signal.unit_minutes != BREAKOUT_UNIT_MINUTES:
        raise DispatchRefused(
            f"signal.unit_minutes must equal {BREAKOUT_UNIT_MINUTES}, "
            f"got {signal.unit_minutes}"
        )
    expected_signal_ts = signal.source_open_time_utc + timedelta(
        minutes=signal.unit_minutes
    )
    if signal.signal_ts_utc != expected_signal_ts:
        raise DispatchRefused(
            f"signal.signal_ts_utc {signal.signal_ts_utc.isoformat()} "
            f"must equal source_open_time_utc + unit_minutes "
            f"({expected_signal_ts.isoformat()})"
        )
    # bool is a subclass of int; a Decimal-typed field must be an exact
    # Decimal — reject float and bool via explicit type-identity.
    if type(bid_fee_rate) is not Decimal:  # noqa: E721
        raise DispatchRefused(
            f"bid_fee_rate must be an exact Decimal, got "
            f"{type(bid_fee_rate).__name__!r}"
        )
    if not bid_fee_rate.is_finite():
        raise DispatchRefused(
            f"bid_fee_rate must be finite, got {bid_fee_rate}"
        )
    if not (Decimal("0") <= bid_fee_rate < Decimal("1")):
        raise DispatchRefused(
            f"bid_fee_rate must satisfy 0 <= rate < 1, got {bid_fee_rate}"
        )
    if not cash.value.is_finite():
        raise DispatchRefused(
            f"cash.value must be finite, got {cash.value}"
        )
    if cash.value < Decimal("0"):
        raise DispatchRefused(
            f"cash.value must be >= 0, got {cash.value}"
        )
    if not position.value.is_finite():
        raise DispatchRefused(
            f"position.value must be finite, got {position.value}"
        )
    if position.value < Decimal("0"):
        raise DispatchRefused(
            f"position.value must be >= 0, got {position.value}"
        )
    if not max_notional_krw.value.is_finite():
        raise DispatchRefused(
            f"max_notional_krw.value must be finite, got "
            f"{max_notional_krw.value}"
        )
    if max_notional_krw.value <= Decimal("0"):
        raise DispatchRefused(
            f"max_notional_krw.value must be > 0, got "
            f"{max_notional_krw.value}"
        )
    if type(stopped_out_lockout) is not bool:  # noqa: E721
        raise DispatchRefused(
            f"stopped_out_lockout must be a bool, got "
            f"{type(stopped_out_lockout).__name__!r}"
        )


def dispatch_breakout_signal(
    *,
    signal: BreakoutSignal,
    cash: Money,
    position: Qty,
    bid_fee_rate: Decimal,
    max_notional_krw: Money,
    stopped_out_lockout: bool,
    observed_at_utc: datetime,
    broker: Broker,
) -> BrokerOrder | None:
    """Submit at most one intent for ``signal`` through ``broker``.

    Returns the resulting ``BrokerOrder`` on submission (including the
    idempotent re-attach case), or ``None`` when the signal is a no-op
    given the current portfolio.
    """
    _validate_inputs(
        signal=signal,
        cash=cash,
        position=position,
        bid_fee_rate=bid_fee_rate,
        max_notional_krw=max_notional_krw,
        stopped_out_lockout=stopped_out_lockout,
        observed_at_utc=observed_at_utc,
    )

    if observed_at_utc < signal.signal_ts_utc:
        raise DispatchRefused(
            f"observed_at_utc {observed_at_utc.isoformat()} precedes "
            f"signal_ts_utc {signal.signal_ts_utc.isoformat()}"
        )

    intent = _build_intent(
        signal=signal,
        cash=cash,
        position=position,
        bid_fee_rate=bid_fee_rate,
        max_notional_krw=max_notional_krw,
        stopped_out_lockout=stopped_out_lockout,
    )
    if intent is None:
        return None

    for existing in broker.list_open():
        if existing.intent != intent:
            raise DispatchRefused(
                f"broker holds unresolved order "
                f"{existing.client_order_id} with a different intent; "
                f"refuse to submit a new one"
            )
    return broker.submit(intent)


def _build_intent(
    *,
    signal: BreakoutSignal,
    cash: Money,
    position: Qty,
    bid_fee_rate: Decimal,
    max_notional_krw: Money,
    stopped_out_lockout: bool,
) -> OrderIntent | None:
    if signal.target_state == "LONG":
        if stopped_out_lockout or position.value != 0:
            return None
        # Full-sleeve pre-fee notional. If the caller instead requested
        # `cash` outright, the venue's bid fee would push the settled
        # spend past the sleeve. Solving `pre_fee * (1 + fee) = cash`
        # keeps the settled spend at exactly `cash` in the frictionless
        # case; the execution engine still owns any qty-step flooring.
        pre_fee_value = cash.value / (Decimal("1") + bid_fee_rate)
        if pre_fee_value <= 0:
            return None
        pre_fee = Money(pre_fee_value)
        if pre_fee > max_notional_krw:
            raise DispatchRefused(
                f"LONG pre-fee notional {pre_fee.value} exceeds "
                f"max_notional_krw {max_notional_krw.value}"
            )
        return OrderIntent(
            side="buy",
            source_open_time_utc=signal.source_open_time_utc,
            unit_minutes=signal.unit_minutes,
            signal_ts_utc=signal.signal_ts_utc,
            requested_notional_krw=pre_fee,
            requested_qty=None,
        )
    # target_state == "CASH"
    if position.value <= 0:
        return None
    return OrderIntent(
        side="sell",
        source_open_time_utc=signal.source_open_time_utc,
        unit_minutes=signal.unit_minutes,
        signal_ts_utc=signal.signal_ts_utc,
        requested_notional_krw=None,
        requested_qty=position,
    )


__all__ = ["DispatchRefused", "dispatch_breakout_signal"]
