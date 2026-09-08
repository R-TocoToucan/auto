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
                                          before M6B (D-68, D-97). Imported
                                          here now so downstream modules
                                          can `except` it starting today;
                                          its raising site is added by
                                          plan 01-02's secrets loader.
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

    D-68 / D-97: the validator MUST report only that the credential *class*
    was detected. Its *value* MUST NEVER appear in any exception message,
    log line, or serialized surface — this exception carries only the
    credential class, deliberately.
    """

    def __init__(self, *, credential_class: str) -> None:
        super().__init__(
            f"Prohibited credential class detected: {credential_class!r}. "
            "Trade-permission credentials are refused pre-M6B (D-68, D-97). "
            "Do NOT include the credential value in this exception or any "
            "log line — class-only reporting is mandatory."
        )
        self.credential_class = credential_class


__all__ = [
    "BithumbBotError",
    "Gate1LoadError",
    "ProhibitedCredentialDetectedError",
    "UnknownCapabilityError",
]
