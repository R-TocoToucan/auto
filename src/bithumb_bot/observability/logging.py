"""Structlog wiring — D-70 defense in depth for the observability pipeline.

Called once at CLI entry (see :mod:`bithumb_bot.cli.main`) before any
handler runs. Idempotent: repeated calls do NOT double-register the
processor chain (structlog's own ``configure`` is idempotent per docs,
but this module also short-circuits if it has already installed its
chain during the current process lifetime — matters for the pytest
suite, where the same interpreter runs many tests).

Processor chain (Finding 11 shape):

1. ``structlog.contextvars.merge_contextvars`` — pulls per-invocation
   context bound via :func:`bind_capability_context` into every event.
2. ``structlog.processors.add_log_level`` — emits the ``level`` key so
   JSONRenderer / ConsoleRenderer both know it.
3. ``structlog.processors.TimeStamper(fmt="iso", utc=True)`` — ISO-8601
   UTC timestamps.
4. :func:`~bithumb_bot.secrets.redaction.redact_secrets` — masks any
   top-level ``SecretStr`` value that leaked into the event dict
   (D-70 belt-and-suspenders to pydantic ``SecretStr`` masking).
5. Renderer:
   * ``fmt="json"`` → ``structlog.processors.JSONRenderer()`` (default;
     production and non-TTY test / CI use).
   * ``fmt="console"`` → ``structlog.dev.ConsoleRenderer(colors=True)``
     (interactive TTY use).

The redactor MUST appear BEFORE the renderer — a masked value that is
already stringified by JSONRenderer cannot be re-inspected as
``SecretStr``.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog

from bithumb_bot.secrets.redaction import redact_secrets

# Module-level flag so the pytest suite (single interpreter, many tests)
# does not re-invoke `structlog.configure` on every call. Structlog's
# own configuration is already idempotent w.r.t. correctness — this
# is a cheap short-circuit to keep test log output stable.
_CONFIGURED: bool = False


def configure_logging(fmt: Literal["json", "console"] = "json") -> None:
    """Configure structlog processors + renderer.

    Args:
        fmt: ``"json"`` (default) for JSON-per-line output — the format
             CI and production headless runs consume. ``"console"`` for
             the human-readable, colored dev-time renderer.

    Idempotent — calling twice is a no-op after the first invocation.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
        _select_renderer(fmt),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def _select_renderer(fmt: Literal["json", "console"]) -> Any:
    if fmt == "json":
        return structlog.processors.JSONRenderer()
    if fmt == "console":
        return structlog.dev.ConsoleRenderer(colors=True)
    raise ValueError(f"unknown fmt={fmt!r} (want 'json' or 'console')")


def bind_capability_context(
    capability: str, command: str, invocation_id: str
) -> None:
    """Bind ``capability`` / ``command`` / ``invocation_id`` to structlog contextvars.

    Every subsequent log call in this asyncio task (or thread, if you
    are not using asyncio) automatically includes these three keys in
    its event dict — this is the D-85 audit-trail vocabulary that the
    CLI dispatcher binds at every dispatch.

    Args:
        capability:    ``"verb.subverb"`` (e.g. ``"m1.fetch-spec"``).
        command:       The exact argv the operator invoked
                       (e.g. ``"bt m1 fetch-spec --market KRW-BTC"``).
        invocation_id: UUID4 hex string identifying this exact invocation.
    """
    structlog.contextvars.bind_contextvars(
        capability=capability,
        command=command,
        invocation_id=invocation_id,
    )


def _reset_for_tests() -> None:
    """Test-only hook to un-set the idempotency flag.

    Real code MUST NOT call this — process-lifetime configuration is
    the whole point of ``_CONFIGURED``. Tests that need to re-configure
    (e.g. to swap ``fmt=json`` -> ``fmt=console`` mid-suite) use this
    helper via ``monkeypatch``.
    """
    global _CONFIGURED
    _CONFIGURED = False


__all__ = ["bind_capability_context", "configure_logging"]
