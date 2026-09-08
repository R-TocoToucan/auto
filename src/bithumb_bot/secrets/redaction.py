"""`redact_secrets` — structlog processor that masks `SecretStr` values.

Placement in the structlog processor chain (wired by plan 01-04):

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            redact_secrets,                        # ← after merge, before render
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

Design:

* Pure function — no I/O, no locking, no state. Structlog processors
  are called on every log event and MUST be cheap.
* Only inspects the top-level ``event_dict``. Nested dicts / lists are
  NOT recursively walked because ``SecretStr`` should never appear
  more than one level deep in a well-designed log call — nested
  containers with secrets are already a design smell we want to make
  visible, not silently mask.
* Importable independently of the full structlog config (no side
  effects on import) so plan 01-04 can wire it in without forcing
  every consumer of ``bithumb_bot.secrets`` to pay the structlog
  import cost.

**D-70 defense in depth.** Pydantic v2's own ``SecretStr`` masking
already covers ``repr``, ``str``, ``model_dump`` and ``model_dump_json``
— that is the primary defense. This processor is the belt to that
suspenders: if a caller accidentally logs a raw ``SecretStr`` (via
``log.info("...", key=settings.account_read_access_key)``) rather than
its unwrapped value, the mask still fires at the observability layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

_MASK = "***"


def redact_secrets(
    logger: object,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor — mask top-level `SecretStr` values in-place.

    Args:
        logger: The structlog logger (unused; part of the processor
            contract).
        method_name: The logging method name (unused; part of the
            processor contract).
        event_dict: The mutable event dict being passed down the
            processor chain.

    Returns:
        The same event dict with every top-level ``SecretStr`` value
        replaced by the literal ``"***"``. Non-``SecretStr`` values are
        untouched.
    """
    for key, value in event_dict.items():
        if isinstance(value, SecretStr):
            event_dict[key] = _MASK
    return event_dict


__all__ = ["redact_secrets"]
