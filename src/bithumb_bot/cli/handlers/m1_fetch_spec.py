"""`bt m1 fetch-spec` handler — REAL implementation (replaces stub).

Sequence:

1. `validate(("m1", "fetch-spec"))` — FIRST STATEMENT (D-85). If not
   ok, print refusal and return 1 without opening any HTTP client.
2. `asyncio.run(fetch_spec(args.market))` — the async orchestrator
   from :mod:`bithumb_bot.bithumb_spec.client`.
3. Print the three output paths on success; return 0.
4. On `httpx.HTTPStatusError`: log only the HTTP status code and a
   strictly-sanitized Bithumb error name (or ``"unknown"``). The
   response body, headers, JWT, keys, query hash and any other
   request/response detail are NEVER read into a log line.
5. On any other raised exception: log the exception class name only;
   print a clean stderr message; return 1.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any

import structlog

from bithumb_bot.config.validator import validate

log = structlog.get_logger()

# A Bithumb API error `name` (per apidocs.bithumb.com) is a short
# machine identifier such as ``jwt_verification`` or
# ``insufficient_funds_ask``. We accept only that shape and length-cap
# defensively — anything else collapses to the sentinel ``"unknown"``
# so a hostile / malformed error body cannot smuggle payload bytes,
# credential fragments, or newlines into a log line.
_BITHUMB_ERROR_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")


def _sanitized_bithumb_error_name(response: Any) -> str:
    """Return the response's Bithumb error `name`, or ``"unknown"``.

    Reads only ``response.json()["error"]["name"]`` and only accepts
    it when it matches :data:`_BITHUMB_ERROR_NAME_PATTERN`. Any parse
    failure, unexpected shape, or non-matching value returns the
    sentinel ``"unknown"`` — never the raw error text, message, body,
    or any other field.
    """
    try:
        body = response.json()
    except Exception:
        return "unknown"
    if not isinstance(body, dict):
        return "unknown"
    err = body.get("error")
    if not isinstance(err, dict):
        return "unknown"
    name = err.get("name")
    if not isinstance(name, str):
        return "unknown"
    if not _BITHUMB_ERROR_NAME_PATTERN.match(name):
        return "unknown"
    return name


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

    # Lazy imports so `bt --help` / `--version` never pull httpx /
    # structlog / PyJWT into the argparse-only code path.
    import httpx

    from bithumb_bot.bithumb_spec.client import fetch_spec

    market = getattr(args, "market", "KRW-BTC")
    try:
        result = asyncio.run(fetch_spec(market))
    except httpx.HTTPStatusError as exc:
        # Strictly sanitized failure surface: HTTP status code + a
        # short allowlisted Bithumb error name, or "unknown". The raw
        # error message, response body, response headers, request
        # headers (which carry the Bearer JWT and query params), keys,
        # query hash and balances are NEVER emitted here.
        http_status = int(exc.response.status_code)
        bithumb_error_name = _sanitized_bithumb_error_name(exc.response)
        log.error(
            "m1.fetch-spec.http_status_error",
            http_status=http_status,
            bithumb_error_name=bithumb_error_name,
        )
        print(
            f"bt m1 fetch-spec: failed (HTTP {http_status}). "
            "See structured logs for details.",
            file=sys.stderr,
        )
        return 1
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
