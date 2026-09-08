"""`bt m1 verify-snapshot` handler — REAL implementation (replaces stub).

Offline verification: MUST NOT construct `BithumbSecrets` (D-89) and
MUST NOT open any HTTP client.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import structlog

from bithumb_bot.config.validator import validate

log = structlog.get_logger()


def handler(args: argparse.Namespace) -> int:
    """Entry point for ``bt m1 verify-snapshot``."""
    _result = validate(("m1", "verify-snapshot"))
    if not _result.ok:
        print(
            f"bt m1 verify-snapshot: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1
    snapshot = getattr(args, "snapshot", None)
    if not snapshot:
        print(
            "bt m1 verify-snapshot: --snapshot <path> is required",
            file=sys.stderr,
        )
        return 1
    snapshot_path = Path(snapshot)

    from bithumb_bot.artifact.canonical import sha256_hex
    from bithumb_bot.bithumb_spec.snapshot import load_snapshot

    try:
        loaded = load_snapshot(snapshot_path)
    except Exception as exc:
        log.error(
            "m1.verify-snapshot.failed",
            error_class=type(exc).__name__,
        )
        print(
            f"bt m1 verify-snapshot: refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    sha_prefix = sha256_hex(snapshot_path.read_bytes())[:12]
    print(f"snapshot:               {snapshot_path}")
    print(f"market:                 {loaded.market}")
    print(f"retrieved_at_utc:       {loaded.retrieved_at_utc}")
    print(f"snapshot_sha256[:12]:   {sha_prefix}")
    print("verification_status:")
    for key in sorted(loaded.verification_status.keys()):
        print(f"  {key:<32} {loaded.verification_status[key]}")
    return 0


__all__ = ["handler"]
