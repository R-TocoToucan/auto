"""Named exceptions used across the `bithumb_bot` package.

Each exception is narrow and named so callers can `except` for the
specific fail-closed condition. Do NOT catch the plain `Exception`
super-class at any registered fail-closed boundary — that would defeat
the D-60 contract.

Exception surface added incrementally by phase:

- Phase 1 / plan 01-01:
  * `Gate1LoadError`                    — TOML parse / pydantic validation
                                          / non-frozen-status failure during
                                          `load_gate1`.
  * `UnknownCapabilityError`            — `REGISTRY[(verb, subverb)]` missed
                                          in `validate()` (D-89 fail hard).
- Phase 1 / plan 01-02:
  * `ProhibitedCredentialDetectedError` — trade-permission credential class
                                          detected in the environment
                                          before M6B (D-68, D-97). Raising
                                          site: `secrets.loader.reject_
                                          trade_credentials`. Class-only
                                          reporting (D-70).
  * `SecretsFileInsideRepoError`        — `BITHUMB_BOT_SECRETS_FILE` env
                                          points at a path that resolves
                                          inside the repository — the file
                                          MUST live outside the repo tree
                                          (D-65, symlink-safe).
  * `AmbiguousSecretsConfigurationError`— the same credential key is
                                          defined in BOTH the OS environment
                                          AND the external secrets file
                                          with DIFFERENT values (D-66
                                          rule 3). Prevents silent
                                          precedence-side selection.
"""

from __future__ import annotations


class BithumbBotError(Exception):
    """Base class for every project-defined exception."""


class Gate1LoadError(BithumbBotError):
    """Raised when `load_gate1` cannot produce a valid frozen `Gate1Decisions`.

    Wraps structural failures — missing file, malformed TOML, pydantic
    ValidationError, or `status != "frozen"`. Always fail-closed per D-60.
    """

    def __init__(self, message: str, *, path: object | None = None) -> None:
        super().__init__(message)
        self.path = path


class UnknownCapabilityError(BithumbBotError):
    """Raised by `validate()` when a capability tuple has no registry entry.

    D-89: unknown commands fail hard — capability requirements never default
    to an empty set. The CLI dispatcher must surface this refusal without
    invoking any handler.
    """


class ProhibitedCredentialDetectedError(BithumbBotError):
    """Raised when a trade-permission credential class is detected pre-M6B.

    D-68 / D-97: the loader / validator MUST report only that the
    credential *class* was detected. Its *value* MUST NEVER appear in any
    exception message, log line, or serialized surface — this exception
    carries only the credential class, deliberately. The constructor
    accepts only `credential_class` as a keyword arg so no future refactor
    can accidentally start passing values through.
    """

    def __init__(self, *, credential_class: str) -> None:
        super().__init__(
            f"Prohibited credential class detected: {credential_class!r}. "
            "Trade-permission credentials are refused pre-M6B (D-68, D-97). "
            "Do NOT include the credential value in this exception or any "
            "log line — class-only reporting is mandatory."
        )
        self.credential_class = credential_class


class SecretsFileInsideRepoError(BithumbBotError):
    """Raised when `BITHUMB_BOT_SECRETS_FILE` resolves inside the repo tree.

    D-65: the external secrets file MUST live outside the repository so
    a stray `git add -f` cannot commit credential material. The check is
    symlink-safe — `Path.resolve(strict=True)` follows the symlink to its
    real target and containment is verified via `Path.is_relative_to` on
    the fully-resolved repo root.

    Carries only the resolved path (so the operator can fix the config)
    — never a credential value.
    """

    def __init__(self, resolved_path: object) -> None:
        super().__init__(
            f"BITHUMB_BOT_SECRETS_FILE resolves to {resolved_path!r}, which "
            "is inside the repository tree. The external secrets file MUST "
            "live outside the repo (D-65). Symlinks are resolved before this "
            "check; place the real file on a path that has no ancestor equal "
            "to the repository root."
        )
        self.resolved_path = resolved_path


class AmbiguousSecretsConfigurationError(BithumbBotError):
    """Raised when a credential key has DIFFERENT values in env AND the file.

    D-66 rule 3: same-key different-value across sources is ambiguous —
    silently choosing one side (env-wins, file-wins) hides a
    configuration drift that could put stale credentials into service.

    Carries only the credential *key name* — never either side's value.
    """

    def __init__(self, credential_key: str) -> None:
        super().__init__(
            f"Credential key {credential_key!r} is defined in BOTH the OS "
            "environment AND the external secrets file with DIFFERENT "
            "values. Precedence is undefined for this state (D-66 rule 3); "
            "remove one source or reconcile the values."
        )
        self.credential_key = credential_key


__all__ = [
    "AmbiguousSecretsConfigurationError",
    "BithumbBotError",
    "Gate1LoadError",
    "ProhibitedCredentialDetectedError",
    "SecretsFileInsideRepoError",
    "UnknownCapabilityError",
]
