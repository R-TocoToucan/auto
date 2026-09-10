"""`bt m1 verify-snapshot` handler — REAL implementation (replaces stub).

Offline verification: MUST NOT construct `BithumbSecrets` (D-89) and
MUST NOT open any HTTP client.

Prints two distinct statuses:

* ``artifact_integrity``  — determined by sidecar + schema verification
  in :func:`~bithumb_bot.bithumb_spec.snapshot.load_snapshot`. Invalid
  → non-zero exit (existing behavior).
* ``execution_readiness`` — determined by
  :func:`~bithumb_bot.execution.readiness.check_execution_readiness`
  under the strict fee policy (``allow_provisional_fee_model=False``).
  The M1 command does not invent slippage, notional cap, or fee-policy
  opt-in values; the strict Boolean is passed inline. Diagnostic only
  in this handler: ``unresolved`` still exits 0 so operators can
  inspect the missing-requirement list. A CI-enforcement option is
  deferred to Batch 2.
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
    from bithumb_bot.execution.readiness import check_execution_readiness

    try:
        loaded = load_snapshot(snapshot_path)
    except Exception as exc:
        log.error(
            "m1.verify-snapshot.failed",
            error_class=type(exc).__name__,
        )
        print(f"artifact_integrity:     invalid")
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

    # Readiness diagnostic — decoupled from artifact integrity. The
    # strict fee policy (``allow_provisional_fee_model=False``) is
    # passed inline: the M1 diagnostic must not invent slippage,
    # notional cap, or a fee-policy opt-in. The strict Boolean here
    # mirrors what a real strategy-evaluation run would accept by
    # default — operators who intend to opt in do so via their own
    # ExecutionConfig at engine call time.
    readiness = check_execution_readiness(
        loaded,
        allow_provisional_fee_model=False,
    )
    missing_str = (
        ", ".join(readiness.missing_requirements)
        if readiness.missing_requirements
        else "(none)"
    )
    print(f"artifact_integrity:     valid")
    print(f"execution_readiness:    {readiness.execution_readiness}")
    print(f"missing_requirements:   {missing_str}")
    return 0


__all__ = ["handler"]
