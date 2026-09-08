"""`bt m1 verify-facts` handler — REAL implementation (replaces stub).

Offline verification: this handler MUST NOT construct
`BithumbSecrets` (D-89) and MUST NOT open any HTTP client.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import structlog

from bithumb_bot.config.validator import validate

log = structlog.get_logger()


def handler(args: argparse.Namespace) -> int:
    """Entry point for ``bt m1 verify-facts``."""
    _result = validate(("m1", "verify-facts"))
    if not _result.ok:
        print(
            f"bt m1 verify-facts: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1
    bundle = getattr(args, "bundle", None)
    if not bundle:
        print(
            "bt m1 verify-facts: --bundle <path> is required",
            file=sys.stderr,
        )
        return 1
    bundle_path = Path(bundle)

    # Lazy import — keep argparse path free of downstream imports.
    from bithumb_bot.bithumb_spec.verification import verify_facts_bundle

    try:
        verify_facts_bundle(bundle_path)
    except Exception as exc:
        log.error(
            "m1.verify-facts.failed",
            error_class=type(exc).__name__,
        )
        print(
            f"bt m1 verify-facts: refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"bundle human-approved: {bundle_path}")
    return 0


__all__ = ["handler"]
