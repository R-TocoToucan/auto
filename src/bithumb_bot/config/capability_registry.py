"""Central capability registry — D-88 guard matrix encoded as data.

D-85: every operation's prerequisites are declared here ONCE. Both the
CLI dispatcher (plan 01-03) and internal service functions (plan 01-04)
must call `validate((verb, subverb))` which consults this table. Direct
Python callers that bypass the CLI cannot bypass the capability check.

D-86: Phase-1 verbs (`config validate`, `m0 selfcheck`, `m1 fetch-spec`,
`m1 verify-facts`, `m1 verify-snapshot`) are registered as functional.

D-87 / D-90: future verbs (`m2 collect-observations`, `m2 calibrate-costs`,
`m2 replay-known-answer`, `m4 evaluate-selection`, `m5 evaluate-module`,
`freeze strategy`, `m6a verify-mock-broker`, `freeze final`,
`holdout evaluate`) are ALSO registered so their prerequisites exist as
data, but their handlers must not be scaffolded as functional. This
module exposes `RESERVED_HANDLER_ERROR` — the CLI dispatcher looks it up
for reserved verbs so refusal is uniform.

D-96: no M6B or live verb is registered.
D-97: every entry has `trade_cred_prohibited=True`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Never


# =============================================================================
# Enums for each requirement dimension (Finding 5 shape)
# =============================================================================


class SnapshotRequirement(Enum):
    """Whether a capability needs an M1 verified spec snapshot at hand.

    NONE                 — no snapshot required at all.
    EVIDENCE_INPUT       — snapshot arrives as an input the command is
                           verifying (e.g. `m1 verify-facts`).
    CANDIDATE            — snapshot is being verified (e.g. `m1 verify-snapshot`).
    VERIFIED_REQUIRED    — a passing sidecar+status snapshot is a
                           precondition (m2+, per D-80).
    """

    NONE = "none"
    EVIDENCE_INPUT = "evidence_input"
    CANDIDATE = "candidate"
    VERIFIED_REQUIRED = "verified_required"


class CapRequirement(Enum):
    """Whether the applicability cap must be numerically frozen (D-89).

    NOT_REQUIRED       — no cap check.
    MAY_BE_NULL        — cap may be null (M2 observation/calibration paths).
    REQUIRED_NON_NULL  — non-null frozen cap required (selection/holdout).
    """

    NOT_REQUIRED = "not_required"
    MAY_BE_NULL = "may_be_null"
    REQUIRED_NON_NULL = "required_non_null"


class CredRequirement(Enum):
    """Which credential class the capability needs (D-67, D-68, D-89).

    NONE                    — no credential loaded.
    ACCOUNT_READ_REQUIRED   — the account/read JWT credential is needed
                              (M1 authenticated read only).
    """

    NONE = "none"
    ACCOUNT_READ_REQUIRED = "account_read_required"


class HumanAuthRequirement(Enum):
    """Human-authorization strength required for this capability.

    NONE                — no human-authorization step needed.
    INVOCATION_ONLY     — CLI invocation itself counts (m1 fetch-spec).
    EXPLICIT_APPROVAL   — explicit approval step before dispatch
                          (freeze strategy, freeze final).
    ONE_TIME_APPROVAL   — a one-time, non-persistent authorization
                          mechanism (holdout evaluate).
    """

    NONE = "none"
    INVOCATION_ONLY = "invocation_only"
    EXPLICIT_APPROVAL = "explicit_approval"
    ONE_TIME_APPROVAL = "one_time_approval"


# =============================================================================
# CapabilityRequirements — one row per registered (verb, subverb)
# =============================================================================


@dataclass(frozen=True)
class CapabilityRequirements:
    """The exact prerequisites for a single capability, per D-88 guard matrix.

    `frozen=True` — instances are immutable; mutation raises
    `FrozenInstanceError`. All eight fields are required (no defaults) so
    every registry row is authored explicitly; there is no "empty
    requirements default" path (D-89).
    """

    gate1: bool
    gate2: bool
    gate3: bool
    snapshot: SnapshotRequirement
    cap: CapRequirement
    cred: CredRequirement
    trade_cred_prohibited: bool  # always True for every registered capability
    human_auth: HumanAuthRequirement


# =============================================================================
# Reserved-handler sentinel (D-90)
# =============================================================================


def RESERVED_HANDLER_ERROR() -> Never:  # noqa: N802 — sentinel is intentionally SHOUT_CASE
    """Handler stub for D-87 reserved future verbs.

    Raises `NotImplementedError` unconditionally. The CLI dispatcher
    (plan 01-03) looks up this sentinel when a reserved verb is invoked
    so every reserved-verb refusal has a single, uniform shape — no
    scaffolded-as-functional code paths (D-90 phase discipline).
    """
    raise NotImplementedError(
        "This verb is reserved for a future phase — do not scaffold it. "
        "Registered in the capability registry per D-87 so its prerequisites "
        "exist as data, but no functional handler is provided in Phase 1 "
        "(D-90 phase discipline)."
    )


# =============================================================================
# REGISTRY — one row per Phase-1 D-86 verb + one row per D-87 reserved verb
# =============================================================================

_PHASE1: dict[tuple[str, str], CapabilityRequirements] = {
    # config validate — validates a candidate gate file; does NOT load creds
    ("config", "validate"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.NONE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    # m0 selfcheck — offline; loads frozen Gate 1
    ("m0", "selfcheck"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.NONE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    # m1 fetch-spec — ONLY registered capability that loads account/read cred
    ("m1", "fetch-spec"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.NONE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.ACCOUNT_READ_REQUIRED,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.INVOCATION_ONLY,
    ),
    # m1 verify-facts — offline; snapshot is EVIDENCE input
    ("m1", "verify-facts"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.EVIDENCE_INPUT,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    # m1 verify-snapshot — offline; snapshot is CANDIDATE being verified
    ("m1", "verify-snapshot"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.CANDIDATE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    # research collect-candles — public REST candle collection, no
    # credentials, invocation-scoped human authorization.
    ("research", "collect-candles"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.NONE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.INVOCATION_ONLY,
    ),
    # research backtest — offline; snapshot is EVIDENCE input for the
    # backtest+evaluation pipeline. No live-broker path.
    ("research", "backtest"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.EVIDENCE_INPUT,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
}


_RESERVED_FUTURE: dict[tuple[str, str], CapabilityRequirements] = {
    # M2 verbs — future phase; snapshot verified-required, cap may be null
    ("m2", "collect-observations"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.MAY_BE_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m2", "calibrate-costs"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.MAY_BE_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m2", "replay-known-answer"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.MAY_BE_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    # M4 / M5 — Gate 2 required, cap required non-null
    ("m4", "evaluate-selection"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m5", "evaluate-module"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    # freeze strategy / freeze final — explicit approval before dispatch
    ("freeze", "strategy"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.EXPLICIT_APPROVAL,
    ),
    ("m6a", "verify-mock-broker"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("freeze", "final"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.EXPLICIT_APPROVAL,
    ),
    # holdout evaluate — one-time non-persistent human authorization
    ("holdout", "evaluate"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=True,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.ONE_TIME_APPROVAL,
    ),
}


REGISTRY: dict[tuple[str, str], CapabilityRequirements] = {
    **_PHASE1,
    **_RESERVED_FUTURE,
}
"""Complete capability registry — D-86 (functional) + D-87 (reserved-but-registered)."""


# Convenience introspection sets used by plan 01-03's dispatcher + tests.
PHASE1_KEYS: frozenset[tuple[str, str]] = frozenset(_PHASE1.keys())
RESERVED_KEYS: frozenset[tuple[str, str]] = frozenset(_RESERVED_FUTURE.keys())


__all__ = [
    "PHASE1_KEYS",
    "REGISTRY",
    "RESERVED_HANDLER_ERROR",
    "RESERVED_KEYS",
    "CapRequirement",
    "CapabilityRequirements",
    "CredRequirement",
    "HumanAuthRequirement",
    "SnapshotRequirement",
]
