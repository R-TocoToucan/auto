"""Venue-agnostic broker interface.

``MockBroker`` is the sole current implementation. A future Bithumb
adapter (M6A+) will implement the same protocol so callers can be
written once against the interface. This module deliberately contains
no HTTP, WebSocket, credential, or environment code — a live broker is
out of milestone scope.
"""

from __future__ import annotations

from typing import Protocol

from bithumb_bot.broker.state import BrokerOrder
from bithumb_bot.execution.intent import OrderIntent


class Broker(Protocol):
    """Minimal venue-agnostic order lifecycle.

    Implementations must be idempotent w.r.t. ``submit``: submitting the
    same ``OrderIntent`` twice returns the same ``BrokerOrder`` and never
    creates a duplicate at the venue.
    """

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        """Persist the intent then return the corresponding order.

        Idempotent — if an order for this intent already exists,
        return it unchanged.
        """
        ...

    def get(self, client_order_id: str) -> BrokerOrder | None:
        """Return the persisted order or ``None`` if unknown."""
        ...

    def list_open(self) -> list[BrokerOrder]:
        """Return every non-terminal order currently persisted."""
        ...

    def cancel(self, client_order_id: str) -> BrokerOrder:
        """Cancel an open order. Fails closed on invalid transitions."""
        ...


__all__ = ["Broker"]
