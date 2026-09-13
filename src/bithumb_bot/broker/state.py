"""Broker order state — the shared vocabulary the MockBroker (and any
future Bithumb adapter) exposes.

Five persisted states (spec):

* ``accepted``          — venue (or mock) acknowledged the intent; no
                          fills yet.
* ``partially_filled``  — at least one fill applied, more still possible.
* ``filled``            — cumulative fills equal the order's terminal
                          intent; terminal.
* ``canceled``          — operator- or venue-initiated cancel; terminal.
* ``rejected``          — venue refused the order (validation or risk);
                          terminal, never fills.

Valid transitions form a small DAG:

    accepted           -> partially_filled | filled | canceled | rejected
    partially_filled   -> partially_filled | filled | canceled
    (terminal)         -> nothing

The transition table and terminal set are the single source of truth
consumed by ``MockBroker`` and by every test that asserts fail-closed
behaviour on invalid transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from bithumb_bot.core.money import Money, Qty
from bithumb_bot.execution.intent import OrderIntent


class OrderState(str, Enum):
    """The five persisted broker order states."""

    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}
)

VALID_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


@dataclass(frozen=True)
class StateTransition:
    """One entry in an order's persisted history."""

    from_state: OrderState | None
    to_state: OrderState
    at_utc: datetime


@dataclass(frozen=True)
class BrokerOrder:
    """Immutable snapshot of a persisted order.

    Every mutation on ``MockBroker`` returns a new ``BrokerOrder``; the
    prior object is never mutated in place.
    """

    client_order_id: str
    intent: OrderIntent
    state: OrderState
    filled_qty: Qty
    filled_notional_krw: Money
    rejection_reason: str | None
    history: tuple[StateTransition, ...]


__all__ = [
    "TERMINAL_STATES",
    "VALID_TRANSITIONS",
    "BrokerOrder",
    "OrderState",
    "StateTransition",
]
