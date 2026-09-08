"""Tests for :mod:`bithumb_bot.observability.logging`.

Structlog's ``testing.capture_logs()`` inserts a ``LogCapture`` processor
that short-circuits the chain — so it does NOT exercise
``redact_secrets``. To verify the redactor is really wired into the
production chain, we take three complementary approaches:

1. Assert ``redact_secrets`` is present in the configured processor list.
2. Insert a custom sink processor AFTER ``redact_secrets`` and verify
   the event dict passed to the sink has the secret masked.
3. Verify ``bind_capability_context`` propagates the three D-85 keys
   (structlog's own ``merge_contextvars`` is exercised inside
   ``capture_logs``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

from bithumb_bot.observability import logging as obs_logging
from bithumb_bot.observability.logging import (
    bind_capability_context,
    configure_logging,
)
from bithumb_bot.secrets.redaction import redact_secrets


@pytest.fixture(autouse=True)
def _reset_logging() -> Any:
    """Reset the idempotency flag + clear contextvars between tests."""
    obs_logging._reset_for_tests()
    structlog.contextvars.clear_contextvars()
    yield
    obs_logging._reset_for_tests()
    structlog.contextvars.clear_contextvars()


class TestConfigureLoggingIdempotent:
    def test_two_calls_do_not_raise(self) -> None:
        configure_logging("json")
        configure_logging("json")  # must not raise
        configure_logging("json")

    def test_console_form_does_not_raise(self) -> None:
        configure_logging("console")  # must not raise

    def test_unknown_fmt_raises(self) -> None:
        with pytest.raises(ValueError):
            configure_logging("yaml")  # type: ignore[arg-type]

    def test_configured_chain_contains_redact_secrets(self) -> None:
        configure_logging("json")
        chain = structlog.get_config()["processors"]
        assert redact_secrets in chain, (
            "redact_secrets MUST be in the configured processor chain "
            "(D-70 belt-and-suspenders to pydantic SecretStr masking)"
        )

    def test_configured_chain_has_merge_contextvars_first(self) -> None:
        configure_logging("json")
        chain = structlog.get_config()["processors"]
        assert chain[0] is structlog.contextvars.merge_contextvars

    def test_redact_secrets_precedes_renderer(self) -> None:
        """The mask MUST be applied before the renderer stringifies the event."""
        configure_logging("json")
        chain = structlog.get_config()["processors"]
        redact_idx = chain.index(redact_secrets)
        # Renderer is the last processor.
        renderer_idx = len(chain) - 1
        assert redact_idx < renderer_idx


class TestRedactSecretsInChain:
    def test_secret_masked_before_sink(self) -> None:
        """Install a sink AFTER redact_secrets; verify the value is `***`."""
        captured: list[dict[str, Any]] = []

        def sink(logger: object, method_name: str, event_dict: dict[str, Any]) -> str:
            captured.append(dict(event_dict))
            return json.dumps(event_dict, default=str)

        # Build the same processor chain configure_logging uses, but swap
        # the renderer for our sink so we can inspect the post-redaction
        # event dict.
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                redact_secrets,
                sink,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(20),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=False,
        )
        log = structlog.get_logger()
        log.info("attempt", secret=SecretStr("sentinel_XYZ_123"))
        assert len(captured) == 1
        assert captured[0]["secret"] == "***"
        # Belt-and-suspenders: the sentinel MUST NOT appear anywhere.
        assert "sentinel_XYZ_123" not in json.dumps(captured, default=str)


def _install_capturing_chain(captured: list[dict[str, Any]]) -> None:
    """Install the production processor chain with a capturing sink last."""

    def sink(logger: object, method_name: str, event_dict: dict[str, Any]) -> str:
        captured.append(dict(event_dict))
        return json.dumps(event_dict, default=str)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            sink,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


class TestBindCapabilityContext:
    def test_bound_context_appears_in_every_event(self) -> None:
        captured: list[dict[str, Any]] = []
        _install_capturing_chain(captured)
        bind_capability_context(
            capability="m0.selfcheck",
            command="bt m0 selfcheck",
            invocation_id="uuid-fixture",
        )
        log = structlog.get_logger()
        log.info("first")
        log.info("second")
        assert len(captured) == 2
        for event in captured:
            assert event["capability"] == "m0.selfcheck"
            assert event["command"] == "bt m0 selfcheck"
            assert event["invocation_id"] == "uuid-fixture"

    def test_bound_context_isolated_across_tests(self) -> None:
        """After the autouse fixture clears contextvars, no leakage."""
        captured: list[dict[str, Any]] = []
        _install_capturing_chain(captured)
        log = structlog.get_logger()
        log.info("event")
        assert "capability" not in captured[0]

    def test_combined_bound_context_and_secret(self) -> None:
        """T-1-04-01 end-to-end — capability + secret on the same event,
        via the same custom-sink pattern as `TestRedactSecretsInChain`."""
        captured: list[dict[str, Any]] = []

        def sink(logger: object, method_name: str, event_dict: dict[str, Any]) -> str:
            captured.append(dict(event_dict))
            return json.dumps(event_dict, default=str)

        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                redact_secrets,
                sink,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(20),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=False,
        )
        bind_capability_context(
            capability="m1.fetch-spec",
            command="bt m1 fetch-spec",
            invocation_id="uuid",
        )
        log = structlog.get_logger()
        log.info("attempt", secret=SecretStr("SENTINEL_MUST_NEVER_APPEAR"))
        assert captured[0]["capability"] == "m1.fetch-spec"
        assert captured[0]["secret"] == "***"
        assert "SENTINEL_MUST_NEVER_APPEAR" not in json.dumps(captured, default=str)
