"""``bt m1 ...`` argparse-facing stubs — REPLACED by plan 01-04 (D-90).

Each stub follows the same defense-in-depth (D-85) pattern used by every
Phase-1 handler:

    def <stub>(args):
        _result = validate((<verb>, <subverb>))   # FIRST STATEMENT
        if not _result.ok:
            ... refuse, return 1 ...
        raise RuntimeError("plan 01-04 required — not yet implemented")

Rationale:

* The dispatcher (plan 01-03-05) needs a callable to bind to each ``m1``
  subverb in its ``HANDLER_MAP`` so ``bt m1 fetch-spec`` argparse-parses
  cleanly today.
* Direct Python callers who bypass the CLI
  (``from bithumb_bot.cli.handlers.m1_stubs import m1_fetch_spec_stub``)
  still trip the ``validate()`` guard first — no bypass path skips
  capability validation (D-85).
* If ``validate()`` passes, the stub body raises ``RuntimeError`` with
  the ``"plan 01-04 required — not yet implemented"`` phrase. This is
  the D-90 discipline for Phase-1 verbs whose functional handler is
  scheduled for a downstream plan: refuse honestly, do not scaffold-as-
  functional.

Plan 01-04 will REPLACE the entire body of each stub with the real
handler; the ``validate()``-first pattern MUST be preserved by the
replacement.

**Defense-in-depth (D-85) — direct Python callers cannot bypass CLI
validation.**
"""

from __future__ import annotations

import argparse
import sys

from bithumb_bot.config.validator import validate


_STUB_RAISE_MESSAGE = (
    "plan 01-04 required — not yet implemented. This stub is a temporary "
    "placeholder registered so the dispatcher's HANDLER_MAP has a callable "
    "for '{verb} {subverb}'. Plan 01-04 replaces this body with the real "
    "handler (D-90 phase discipline)."
)


def _refuse(capability: tuple[str, str], reason: str, missing: tuple[str, ...]) -> int:
    """Print a uniform refusal message + return 1 (defense in depth)."""
    verb, subverb = capability
    print(
        f"bt {verb} {subverb}: refusal: {reason} "
        f"(missing: {', '.join(missing) or 'unspecified'})",
        file=sys.stderr,
    )
    return 1


def m1_fetch_spec_stub(args: argparse.Namespace) -> int:
    """Stub for ``bt m1 fetch-spec`` — plan 01-04 replaces this body.

    Defense-in-depth (D-85): FIRST STATEMENT is ``validate()`` — direct
    Python callers cannot bypass it.
    """
    _result = validate(("m1", "fetch-spec"))
    if not _result.ok:
        return _refuse(("m1", "fetch-spec"), _result.reason or "unspecified", _result.missing)
    raise RuntimeError(_STUB_RAISE_MESSAGE.format(verb="m1", subverb="fetch-spec"))


def m1_verify_facts_stub(args: argparse.Namespace) -> int:
    """Stub for ``bt m1 verify-facts`` — plan 01-04 replaces this body."""
    _result = validate(("m1", "verify-facts"))
    if not _result.ok:
        return _refuse(("m1", "verify-facts"), _result.reason or "unspecified", _result.missing)
    raise RuntimeError(_STUB_RAISE_MESSAGE.format(verb="m1", subverb="verify-facts"))


def m1_verify_snapshot_stub(args: argparse.Namespace) -> int:
    """Stub for ``bt m1 verify-snapshot`` — plan 01-04 replaces this body."""
    _result = validate(("m1", "verify-snapshot"))
    if not _result.ok:
        return _refuse(("m1", "verify-snapshot"), _result.reason or "unspecified", _result.missing)
    raise RuntimeError(_STUB_RAISE_MESSAGE.format(verb="m1", subverb="verify-snapshot"))


__all__ = [
    "m1_fetch_spec_stub",
    "m1_verify_facts_stub",
    "m1_verify_snapshot_stub",
]
