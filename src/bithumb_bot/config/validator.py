"""Capability-scoped, fail-closed startup validator (SAFE-02 restructured per D-99).

`validate((verb, subverb))` — look up the capability's requirements in the
registry (D-88), load ONLY the gate files it needs (D-59), and short-circuit
on the first missing prerequisite. Never partial-succeed.

D-60: any missing/malformed/hash-mismatched required input is a fail-closed
condition — the function returns `ValidationResult(ok=False, missing=(...),
reason=<specific message>)`, never a bare "gate1 missing" placeholder when a
richer parse error is available.

D-56: this validator MUST NOT create `gate2.toml` or `gate3.toml`. A missing
future-gate file returns `ok=False` with the missing tuple containing
`"gate2"` / `"gate3"`; those files are created only after their respective
approved freezes in Phase 3 / Phase 5.

D-89: an unknown capability raises `UnknownCapabilityError` — capability
requirements never default to an empty set.

D-97: trade-credential prohibition is enforced via
:func:`bithumb_bot.secrets.loader.reject_trade_credentials`.

Batch 2 enforcement — every requirement dimension declared in the
capability registry MUST either be enforced here or explicitly refused
fail-closed as an "unsupported requirement" so no reserved handler runs
believing an unenforced precondition was satisfied.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from bithumb_bot.config.capability_registry import (
    REGISTRY,
    CapabilityRequirements,
    CapRequirement,
    CredRequirement,
    HumanAuthRequirement,
    SnapshotRequirement,
)
from bithumb_bot.config.gate_loader import load_gate1
from bithumb_bot.errors import (
    Gate1LoadError,
    ProhibitedCredentialDetectedError,
    UnknownCapabilityError,
)
from bithumb_bot.secrets.loader import (
    load_secrets,
    reject_trade_credentials,
)


# ---------------------------------------------------------------------------
# Env-var conventions
# ---------------------------------------------------------------------------

#: Env var used to override the repo-root for gate-file lookups.
REPO_ROOT_ENV = "BITHUMB_BOT_REPO_ROOT"

#: D-67 credential-class env vars.
ACCOUNT_READ_ENV_VARS: tuple[str, str] = (
    "BITHUMB_ACCOUNT_READ_ACCESS_KEY",
    "BITHUMB_ACCOUNT_READ_SECRET_KEY",
)

#: D-67 trade-credential env vars — MUST NEVER be loaded pre-M6B.
TRADE_ENV_VARS: tuple[str, str] = (
    "BITHUMB_TRADE_ACCESS_KEY",
    "BITHUMB_TRADE_SECRET_KEY",
)


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a `validate(capability)` call."""

    ok: bool
    missing: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


def _resolve_repo_root() -> Path:
    env = os.environ.get(REPO_ROOT_ENV)
    if env:
        return Path(env)
    return Path.cwd()


def _check_trade_credential_prohibition(repo_root: Path) -> ValidationResult | None:
    """Return a failing ValidationResult iff a trade-cred env var is set.

    Value is never inspected — only the credential *class* is reported.
    """
    try:
        settings = load_secrets(repo_root)
        reject_trade_credentials(settings)
    except ProhibitedCredentialDetectedError as exc:
        return ValidationResult(
            ok=False,
            missing=("trade_credential_prohibited",),
            reason=(
                f"Trade credential class detected in environment "
                f"({exc.credential_class!r}) — refused (D-68 / D-97). "
                "Value not inspected."
            ),
        )
    return None


def _check_cred(
    row: CapabilityRequirements, repo_root: Path
) -> ValidationResult | None:
    """Return a failing result iff the capability's required credential is absent.

    Uses :func:`bithumb_bot.secrets.loader.load_secrets` as the single
    source of truth — a credential supplied only through an approved
    external secrets file is accepted the same as an env-var value.
    Only the fact of presence is inspected; values are never read.
    """
    if row.cred is CredRequirement.NONE:
        return None
    if row.cred is CredRequirement.ACCOUNT_READ_REQUIRED:
        try:
            settings = load_secrets(repo_root)
        except ProhibitedCredentialDetectedError:
            # The dedicated trade-cred check runs first; if we're here
            # a load_secrets exception is a genuine config error.
            raise
        if (
            settings.account_read_access_key is None
            or settings.account_read_secret_key is None
        ):
            return ValidationResult(
                ok=False,
                missing=("account_read_cred",),
                reason=(
                    "account/read credential class is required for this "
                    "capability but neither the OS environment nor the "
                    "external secrets file (BITHUMB_BOT_SECRETS_FILE) "
                    "supplied both fields (values not inspected)."
                ),
            )
        return None
    raise UnknownCapabilityError(
        f"Unhandled CredRequirement value {row.cred!r}. Extend validator._check_cred."
    )


def _check_gate1(repo_root: Path) -> ValidationResult | None:
    path = repo_root / "config" / "decisions" / "gate1.toml"
    try:
        load_gate1(path)
    except Gate1LoadError as exc:
        return ValidationResult(
            ok=False,
            missing=("gate1",),
            reason=str(exc),
        )
    return None


def _check_future_gate(
    gate_name: str, repo_root: Path
) -> ValidationResult | None:
    """Check for the presence of a future gate file WITHOUT creating it (D-56)."""
    path = repo_root / "config" / "decisions" / f"{gate_name}.toml"
    if path.is_file():
        return None
    phase_map = {"gate2": "Phase 3", "gate3": "Phase 5"}
    return ValidationResult(
        ok=False,
        missing=(gate_name,),
        reason=(
            f"{gate_name} not yet frozen — {phase_map[gate_name]}. This file "
            "is created only after its approved freeze; see D-56 (Phase 1 "
            "MUST NOT create it)."
        ),
    )


# --- Batch 2: enforcement branches for the remaining requirement dimensions ---

#: Snapshot-requirement enforcement map. ``None`` = no additional
#: precondition; the handler consumes the snapshot via its own typed
#: load path (``load_snapshot`` / ``verify_facts_bundle``). A tuple =
#: refuse fail-closed with the given missing token + reason.
_SNAPSHOT_ENFORCEMENT: dict[SnapshotRequirement, tuple[str, str] | None] = {
    SnapshotRequirement.NONE: None,
    SnapshotRequirement.EVIDENCE_INPUT: None,
    SnapshotRequirement.CANDIDATE: None,
    SnapshotRequirement.VERIFIED_REQUIRED: (
        "snapshot_verified_required",
        "capability requires a verified spec snapshot precondition, "
        "which has no Phase-1 enforcement path. Refused fail-closed "
        "until the requirement is implemented (D-90 phase discipline).",
    ),
}

#: Applicability-cap enforcement map.
_CAP_ENFORCEMENT: dict[CapRequirement, tuple[str, str] | None] = {
    CapRequirement.NOT_REQUIRED: None,
    CapRequirement.MAY_BE_NULL: (
        "cap_may_be_null_precondition",
        "capability's applicability-cap policy has no Phase-1 "
        "enforcement path. Refused fail-closed (D-90 phase discipline).",
    ),
    CapRequirement.REQUIRED_NON_NULL: (
        "cap_required_non_null",
        "capability requires a frozen, non-null applicability cap "
        "(Gate 2). No Phase-1 enforcement path exists — refused "
        "fail-closed until Gate 2 is frozen (D-90 phase discipline).",
    ),
}

#: Human-authorization enforcement map.
_HUMAN_AUTH_ENFORCEMENT: dict[HumanAuthRequirement, tuple[str, str] | None] = {
    HumanAuthRequirement.NONE: None,
    HumanAuthRequirement.INVOCATION_ONLY: None,
    HumanAuthRequirement.EXPLICIT_APPROVAL: (
        "human_auth_explicit_approval",
        "capability requires an invocation-scoped explicit human "
        "approval step, which has no Phase-1 enforcement path. "
        "Refused fail-closed (D-90 phase discipline).",
    ),
    HumanAuthRequirement.ONE_TIME_APPROVAL: (
        "human_auth_one_time_approval",
        "capability requires a one-time, non-persistent human "
        "authorization step (holdout evaluate), which has no Phase-1 "
        "enforcement path. Refused fail-closed (D-90 phase discipline).",
    ),
}


def _check_snapshot(row: CapabilityRequirements) -> ValidationResult | None:
    entry = _SNAPSHOT_ENFORCEMENT.get(row.snapshot)
    if entry is None:
        return None
    token, reason = entry
    return ValidationResult(ok=False, missing=(token,), reason=reason)


def _check_cap(row: CapabilityRequirements) -> ValidationResult | None:
    entry = _CAP_ENFORCEMENT.get(row.cap)
    if entry is None:
        return None
    token, reason = entry
    return ValidationResult(ok=False, missing=(token,), reason=reason)


def _check_human_auth(row: CapabilityRequirements) -> ValidationResult | None:
    entry = _HUMAN_AUTH_ENFORCEMENT.get(row.human_auth)
    if entry is None:
        return None
    token, reason = entry
    return ValidationResult(ok=False, missing=(token,), reason=reason)


def validate(capability: tuple[str, str]) -> ValidationResult:
    """Validate every prerequisite for `capability` per the D-88 guard matrix."""
    row = REGISTRY.get(capability)
    if row is None:
        raise UnknownCapabilityError(
            f"capability {capability!r} is not registered. Unknown commands "
            "fail hard; capability requirements never default to an empty set (D-89)."
        )

    repo_root = _resolve_repo_root()

    if row.trade_cred_prohibited:
        trade_result = _check_trade_credential_prohibition(repo_root)
        if trade_result is not None:
            return trade_result

    if row.gate1:
        result = _check_gate1(repo_root)
        if result is not None:
            return result

    if row.gate2:
        result = _check_future_gate("gate2", repo_root)
        if result is not None:
            return result

    if row.gate3:
        result = _check_future_gate("gate3", repo_root)
        if result is not None:
            return result

    result = _check_cred(row, repo_root)
    if result is not None:
        return result

    # Remaining requirement dimensions — every enum value declared in
    # the registry is either enforced or refused fail-closed via the
    # tables above. Checked after gate/cred so a missing gate2/gate3
    # or credential still surfaces first.
    for check in (_check_snapshot, _check_cap, _check_human_auth):
        result = check(row)
        if result is not None:
            return result

    return ValidationResult(ok=True)


__all__ = [
    "ACCOUNT_READ_ENV_VARS",
    "REPO_ROOT_ENV",
    "TRADE_ENV_VARS",
    "ValidationResult",
    "validate",
]
