"""`bt m1 verify-snapshot` handler — REAL implementation (replaces stub).

Offline verification: MUST NOT construct `BithumbSecrets` (D-89) and
MUST NOT open any HTTP client.

Prints three distinct statuses (Batch 1B):

* ``artifact_integrity``            — determined by sidecar + schema
  verification in :func:`~bithumb_bot.bithumb_spec.snapshot.load_snapshot`.
  Invalid → non-zero exit.
* ``research_simulation_readiness`` — determined by
  :func:`~bithumb_bot.execution.readiness.check_execution_readiness`
  under the strict fee policy (``allow_provisional_fee_model=False``)
  and with no research quantum supplied at this diagnostic layer — the
  M1 command does not invent research assumptions. Operators enable
  research readiness by passing an :class:`~bithumb_bot.execution.
  config.ExecutionConfig` with the quantum + opt-in at the caller.
* ``live_execution_readiness``      — strict live-surface diagnostic
  (M6B).

Default invocation exits 0 while clearly reporting unresolved
readiness so operators can inspect the missing-requirement list. The
``--require-execution-ready`` flag turns unresolved **research**
readiness into a non-zero exit for CI/gate use. Invalid artifacts
always exit non-zero regardless of the flag.
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
    require_ready = bool(getattr(args, "require_execution_ready", False))

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
        print(f"artifact_integrity:              invalid")
        print(
            f"bt m1 verify-snapshot: refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    sha_prefix = sha256_hex(snapshot_path.read_bytes())[:12]
    print(f"snapshot:                        {snapshot_path}")
    print(f"market:                          {loaded.market}")
    print(f"retrieved_at_utc:                {loaded.retrieved_at_utc}")
    print(f"snapshot_sha256[:12]:            {sha_prefix}")
    print("verification_status:")
    for key in sorted(loaded.verification_status.keys()):
        print(f"  {key:<32} {loaded.verification_status[key]}")

    readiness = check_execution_readiness(
        loaded,
        allow_provisional_fee_model=False,
        simulation_quantity_quantum=None,
    )
    research_missing = (
        ", ".join(readiness.research_missing_requirements)
        if readiness.research_missing_requirements
        else "(none)"
    )
    live_missing = (
        ", ".join(readiness.live_missing_requirements)
        if readiness.live_missing_requirements
        else "(none)"
    )
    print(f"artifact_integrity:              valid")
    print(
        f"research_simulation_readiness:   "
        f"{readiness.research_simulation_readiness}"
    )
    print(f"research_missing_requirements:   {research_missing}")
    print(
        f"live_execution_readiness:        "
        f"{readiness.live_execution_readiness}"
    )
    print(f"live_missing_requirements:       {live_missing}")
    if require_ready and readiness.research_simulation_readiness != "ready":
        print(
            f"bt m1 verify-snapshot: --require-execution-ready failed "
            f"(research_simulation_readiness="
            f"{readiness.research_simulation_readiness}; "
            f"missing={research_missing})",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = ["handler"]
