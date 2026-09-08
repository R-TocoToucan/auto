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

D-97: trade-credential prohibition is enforced by `secrets.loader.
reject_trade_credentials` — this module invokes it as its final check
for every capability with `trade_cred_prohibited=True` (which is every
registered capability per D-97). On rejection this validator turns the
raised `ProhibitedCredentialDetectedError` into a clean
`ValidationResult(ok=False, missing=("trade_credential_prohibited",),
reason=...)` so the CLI dispatcher can print a uniform refusal instead
of a Python traceback. The actual credential value is never inspected
anywhere in this call chain (D-68 / D-70).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from bithumb_bot.config.capability_registry import (
    REGISTRY,
    CapabilityRequirements,
    CredRequirement,
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

#: Env var used to override the repo-root for gate-file lookups. Tests set this
#: so they can point the validator at a `tmp_path`-rooted config tree. In
#: production the validator resolves the repo root via ``Path.cwd()``.
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
    """Outcome of a `validate(capability)` call.

    Attributes:
        ok:       True iff every prerequisite for the requested capability
                  was satisfied. False on the first missing prerequisite.
        missing:  A tuple listing the specific prerequisites that could not
                  be satisfied. Ordered by check order; the first entry is
                  the one that short-circuited.
        reason:   A human-readable message explaining the specific failure
                  (parse-error text, missing-env-var name, etc.). None
                  when ok=True.
    """

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

    D-68 / D-97: trade-permission credentials are prohibited pre-M6B for
    every registered capability. Only the credential *class* is reported —
    the value is never inspected.

    Delegates to `bithumb_bot.secrets.loader.reject_trade_credentials`
    (task 01-02-05). When that raises `ProhibitedCredentialDetectedError`
    we translate it into a clean `ValidationResult(ok=False, ...)` so
    the CLI dispatcher can print a uniform refusal string. The reason
    text carries only the credential class ("trade") — no substring of
    the value is ever spliced in (D-70).
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


def _check_cred(row: CapabilityRequirements) -> ValidationResult | None:
    """Return a failing result iff the capability's required env vars are absent.

    Never reads the credential VALUES — only checks env-var presence
    (D-89 / D-70).
    """
    if row.cred is CredRequirement.NONE:
        return None
    if row.cred is CredRequirement.ACCOUNT_READ_REQUIRED:
        missing = [name for name in ACCOUNT_READ_ENV_VARS if not os.environ.get(name)]
        if missing:
            return ValidationResult(
                ok=False,
                missing=("account_read_cred",),
                reason=(
                    "account/read credential class is required for this capability "
                    f"but the following env vars are absent (values not inspected): "
                    f"{', '.join(missing)}."
                ),
            )
        return None
    # Defensive: enum extended in the future must be handled explicitly.
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
        # Future work: parse + validate. Phase 1 only checks presence per
        # plan Behavior contract.
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


def validate(capability: tuple[str, str]) -> ValidationResult:
    """Validate every prerequisite for `capability` per the D-88 guard matrix.

    Args:
        capability: `(verb, subverb)` tuple. MUST be a registered key —
            unknown capabilities raise `UnknownCapabilityError` (D-89).

    Returns:
        ValidationResult(ok=True, ...) iff every prereq is satisfied.
        ValidationResult(ok=False, missing=(...), reason=...) on the first
        failing prereq (short-circuit — never partial-succeed).

    Raises:
        UnknownCapabilityError: capability not in REGISTRY (D-89).
    """
    row = REGISTRY.get(capability)
    if row is None:
        raise UnknownCapabilityError(
            f"capability {capability!r} is not registered. Unknown commands "
            "fail hard; capability requirements never default to an empty set (D-89)."
        )

    repo_root = _resolve_repo_root()

    # Trade-cred prohibition (D-97) — applies to every registered capability
    # pre-M6B. Translated from ProhibitedCredentialDetectedError into a
    # clean ValidationResult so the CLI dispatcher (plan 01-03) can print
    # a uniform refusal string. Value is never inspected (D-70).
    if row.trade_cred_prohibited:
        trade_result = _check_trade_credential_prohibition(repo_root)
        if trade_result is not None:
            return trade_result

    # Ordering of checks: gate1 → gate2 → gate3 → cred. First failing prereq
    # short-circuits (D-59: load only what's required).
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

    result = _check_cred(row)
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
