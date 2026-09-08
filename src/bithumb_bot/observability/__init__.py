"""`bithumb_bot.observability` — structured logging wiring.

Public surface is intentionally small: :func:`configure_logging`
(idempotent process-start setup) and
:func:`bind_capability_context` (per-invocation context binding).

The ``redact_secrets`` structlog processor (from
:mod:`bithumb_bot.secrets.redaction`) is placed in the processor chain
here so any log line that accidentally receives a raw ``SecretStr``
still leaves the pipeline as ``"***"`` — belt-and-suspenders to
pydantic v2's built-in ``SecretStr`` ``repr``/``str``/JSON masking.
"""

from __future__ import annotations

from bithumb_bot.observability.logging import (
    bind_capability_context,
    configure_logging,
)

__all__ = ["bind_capability_context", "configure_logging"]
