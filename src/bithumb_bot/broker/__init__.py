"""Broker adapters.

Import Linter forbidden-targets (D-71): ``bithumb_bot.core`` and
``bithumb_bot.paper`` may NOT import anything from this package. This
first landing carries the venue-agnostic ``Broker`` protocol and the
persistent, idempotent ``MockBroker`` — a filesystem-backed simulator
with no HTTP, WebSocket, credential, or environment access. A real
Bithumb adapter lands in Phase 5 M6A and remains out of milestone
scope for now.
"""

from bithumb_bot.broker.interface import Broker
from bithumb_bot.broker.mock import (
    BrokerError,
    BrokerStateCorrupt,
    FillExceedsIntent,
    InvalidStateTransition,
    MockBroker,
    deterministic_client_order_id,
)
from bithumb_bot.broker.state import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    BrokerOrder,
    OrderState,
    StateTransition,
)

__all__ = [
    "TERMINAL_STATES",
    "VALID_TRANSITIONS",
    "Broker",
    "BrokerError",
    "BrokerOrder",
    "BrokerStateCorrupt",
    "FillExceedsIntent",
    "InvalidStateTransition",
    "MockBroker",
    "OrderState",
    "StateTransition",
    "deterministic_client_order_id",
]
