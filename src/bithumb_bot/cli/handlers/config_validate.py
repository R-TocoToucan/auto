"""``bt config validate --through gate1`` handler (D-86, D-89).

Behavior:

* Defense-in-depth per D-85: FIRST STATEMENT calls
  :func:`bithumb_bot.config.validator.validate` — direct Python callers
  cannot bypass CLI validation.
* Loads ``config/decisions/gate1.toml`` via
  :func:`bithumb_bot.config.gate_loader.load_gate1` (the same loader the
  validator would use) as an **inspect-only** operation (D-89: config
  validate may inspect a candidate but must NOT mark it approved). The
  handler never writes to disk and never touches the gate file's mtime.
* Prints a structured summary of the loaded decisions via the pure
  :func:`format_gate1_summary` function (easy to assert on in tests
  without capsys/capfd).
* Returns ``0`` on success, ``1`` on validator refusal. A ``Gate1LoadError``
  propagates for the dispatcher to translate into a clean stderr message
  (see :func:`bithumb_bot.cli.dispatcher.dispatch`'s
  ``_TRANSLATED_EXCEPTIONS`` tuple).

The summary NEVER prints a credential-adjacent field — there are none in
``gate1.toml`` by construction (D-57 forbids them in the committed
Decision Register), and the test suite asserts the SUMMARY does not
match ``/access|secret|token|withdraw/i`` (D-69: no withdrawal path,
no withdrawal-adjacent field, ever).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bithumb_bot.config.gate1_model import Gate1Decisions
from bithumb_bot.config.gate_loader import load_gate1
from bithumb_bot.config.validator import validate


# Repo root — module-level constant so tests can `mock.patch()` it to
# redirect at a `tmp_path`-rooted config tree. Production callers get
# ``Path.cwd()``; tests get the injected path via `monkeypatch` or the
# ``mock.patch`` shown in ``test_handlers_config_validate.py``.
REPO_ROOT: Path = Path.cwd()


# Fields authored as inline-table sentinels in gate1.toml — these are the
# "value-deferred to Gate 2 / Gate 3" fields (D-41, D-42, plus D-09
# `max_validated_notional_krw`). The count is a stable summary figure.
_VALUE_DEFERRED_FIELDS: tuple[str, ...] = (
    # D-09 applicability cap
    "max_validated_notional_krw",
    # D-41 Gate-2 fields (8)
    "max_received_trade_delivery_lag_ms",
    "public_ws_transport_liveness_timeout_ms",
    "fallback_rest_poll_interval_ms",
    "trigger_rest_connect_timeout_ms",
    "trigger_rest_read_timeout_ms",
    "max_unverified_interval_ms",
    "ws_recovery_stability_window_ms",
    "ws_recovery_min_valid_events",
    # D-42 Gate-3 fields (7)
    "watchdog_heartbeat_interval_ms",
    "watchdog_lease_ttl_ms",
    "ws_reconnect_backoff_initial_ms",
    "ws_reconnect_backoff_cap_ms",
    "ws_reconnect_jitter_policy",
    "per_reconnect_cycle_attempt_limit",
    "reconnect_circuit_breaker_window_ms",
)


def format_gate1_summary(gate1: Gate1Decisions, gate1_sha256: str) -> str:
    """Render the Gate-1 summary printed by ``bt config validate --through gate1``.

    Pure function — accepts the parsed decisions and the provenance hash,
    returns a formatted string. Tests assert on the return value directly
    rather than capturing stdout.

    Args:
        gate1:        Parsed and validated :class:`Gate1Decisions` model.
        gate1_sha256: 64-char hex SHA-256 of the file's committed bytes.

    Returns:
        Multi-line summary string; NEVER contains any credential-adjacent
        substring (assertion covered by
        ``test_summary_never_prints_credential_tokens``).
    """
    value_deferred_count = sum(
        1
        for field_name in _VALUE_DEFERRED_FIELDS
        if getattr(gate1, field_name, None) is None
    )
    provisional = gate1.provisional_engineering_notional_krw
    lines = [
        "gate: gate1",
        f"status: {gate1.status}",
        f"schema_version: {gate1.schema_version}",
        f"source_commit: {gate1.source_commit[:12]}",
        f"research_spec_sha256: {gate1.research_spec_sha256[:12]}",
        f"execution_spec_sha256: {gate1.execution_spec_sha256[:12]}",
        f"gate1_file_sha256: {gate1_sha256[:12]}",
        f"provisional_engineering_notional_krw: {provisional}",
        f"value-deferred fields awaiting Gate-2/Gate-3: {value_deferred_count}",
    ]
    return "\n".join(lines) + "\n"


def handler(args: argparse.Namespace) -> int:
    """`bt config validate --through gate1` handler entry point.

    Defense-in-depth (D-85) — FIRST STATEMENT calls ``validate()`` so
    direct Python callers bypass nothing.
    """
    _result = validate(("config", "validate"))
    if not _result.ok:
        print(
            f"bt config validate: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    # `args.through` is guaranteed by argparse to be "gate1" (only choice).
    # Explicitly consult it here so a future extension that accepts
    # additional gates can branch cleanly.
    through = getattr(args, "through", "gate1")
    if through != "gate1":
        print(
            f"bt config validate: unsupported --through value {through!r}; "
            "only 'gate1' is accepted in Phase 1 (D-89).",
            file=sys.stderr,
        )
        return 1

    gate1_path = REPO_ROOT / "config" / "decisions" / "gate1.toml"
    # `load_gate1` may raise `Gate1LoadError`; the dispatcher's
    # `_TRANSLATED_EXCEPTIONS` tuple catches and translates it to a
    # clean stderr message. Do NOT catch here — that would swallow the
    # named-exception surface the dispatcher relies on.
    gate1, gate1_sha256 = load_gate1(gate1_path)

    print(format_gate1_summary(gate1, gate1_sha256))
    return 0


__all__ = ["REPO_ROOT", "format_gate1_summary", "handler"]
