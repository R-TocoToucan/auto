"""`bt m1 fetch-spec` handler — REAL implementation (replaces stub).

Sequence:

1. `validate(("m1", "fetch-spec"))` — FIRST STATEMENT (D-85). If not
   ok, print refusal and return 1 without opening any HTTP client.
2. `asyncio.run(fetch_spec(args.market))` — the async orchestrator
   from :mod:`bithumb_bot.bithumb_spec.client`.
3. Print the three output paths on success; return 0.
4. On any raised exception: log via structlog (secrets already
   redacted via the wired processor); print a clean stderr message
   with a bundle-directory hint if the bundle was written; return 1.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from bithumb_bot.config.validator import validate

log = structlog.get_logger()


def handler(args: argparse.Namespace) -> int:
    """Entry point for ``bt m1 fetch-spec``."""
    _result = validate(("m1", "fetch-spec"))
    if not _result.ok:
        print(
            f"bt m1 fetch-spec: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    # Lazy import so `bt --help` / `--version` never pulls httpx /
    # structlog / PyJWT into the argparse-only code path.
    from bithumb_bot.bithumb_spec.client import fetch_spec

    market = getattr(args, "market", "KRW-BTC")
    try:
        result = asyncio.run(fetch_spec(market))
    except Exception as exc:
        log.error(
            "m1.fetch-spec.failed",
            error_class=type(exc).__name__,
            # Deliberately NOT logging str(exc) — the codebase's D-70
            # exceptions carry no credential material, but a bare
            # Bithumb error message could reveal an internal detail.
        )
        print(
            f"bt m1 fetch-spec: failed ({type(exc).__name__}). "
            "See structured logs for details.",
            file=sys.stderr,
        )
        return 1
    print(f"snapshot:              {result.snapshot_path}")
    print(f"sanitized fixture:     {result.fixture_path}")
    print(f"verification bundle:   {result.verification_bundle_dir}")
    return 0


__all__ = ["handler"]
