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
- Phase 1 / plan 01-04:
  * `SnapshotAlreadyConsumedError`      — an atomic-write / sidecar target
                                          already exists on disk AND its
                                          sidecar-recorded hash matches
                                          the on-disk file's hash (D-76
                                          "never overwrite a previously
                                          consumed snapshot").
  * `CriticalCorruptionAlert`           — an atomic-write target already
                                          exists AND its sidecar-recorded
                                          hash does NOT match the on-disk
                                          file — the on-disk file is
                                          renamed to `_corrupt_<ts>_...`
                                          and this exception is raised
                                          loudly (T-1-04-09).
  * `SidecarHashMismatchError`          — a snapshot load re-computed the
                                          SHA-256 of the on-disk bytes and
                                          it did not match the sidecar
                                          value; the snapshot has been
                                          tampered or truncated. Raise
                                          site: `snapshot.load_snapshot`.
  * `SnapshotValidationError`           — a snapshot passed the sidecar
                                          hash check but its structure
                                          (schema version, required
                                          verification_status keys) is
                                          invalid per D-80.
  * `AuthConstructionError`             — an exception was raised while
                                          building a JWT bearer token.
                                          The exception carries only the
                                          string ``see structured logs``
                                          — the underlying error text and
                                          any credential material remain
                                          out of the exception message
                                          per D-70 / T-1-04-01.
  * `UnresolvedFactError`               — a `VERIFICATION.md` bundle was
                                          submitted for validation with
                                          at least one required
                                          build-time fact still in the
                                          ``unresolved`` state. The
                                          verifier refuses to advance
                                          per D-78.
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


class SnapshotAlreadyConsumedError(BithumbBotError):
    """Raised when an atomic-write target already exists with a matching sidecar.

    D-76: "never overwrite a previously consumed snapshot". The
    ``guard_against_overwrite`` primitive raises this exception when the
    target path exists AND its sidecar SHA-256 matches the on-disk file
    — i.e. the previously written snapshot is intact and MUST NOT be
    silently replaced.
    """

    def __init__(self, target: object) -> None:
        super().__init__(
            f"Refusing to overwrite previously consumed snapshot at "
            f"{target!r} (D-76). Delete the existing file explicitly if "
            "you truly want to replace it."
        )
        self.target = target


class CriticalCorruptionAlert(BithumbBotError):
    """Raised when an existing snapshot's on-disk bytes do NOT match its sidecar.

    T-1-04-09 branch (b): the target path exists but the sidecar hash
    disagrees with the on-disk file. This is a critical integrity
    condition: the file is renamed to ``_corrupt_<ts>_<name>`` and this
    exception is raised so the operator is alerted rather than silently
    overwriting corrupt evidence.
    """

    def __init__(self, corrupt_path: object) -> None:
        super().__init__(
            f"CRITICAL: existing snapshot at {corrupt_path!r} does NOT "
            "match its sidecar hash. The file has been renamed for forensic "
            "review; do NOT overwrite. Investigate the source of corruption "
            "before writing a new snapshot in the same location."
        )
        self.corrupt_path = corrupt_path


class SidecarHashMismatchError(BithumbBotError):
    """Raised on `load_snapshot` when the recomputed SHA-256 differs from the sidecar.

    T-1-04-03: the snapshot file has been tampered, truncated, or
    otherwise mutated after its sidecar was written. The loader refuses
    to hand the parsed model to the simulator. Carries the path only.
    """

    def __init__(self, path: object) -> None:
        super().__init__(
            f"Snapshot at {path!r} failed sidecar hash verification. "
            "The on-disk bytes do not match the recorded SHA-256; the "
            "snapshot MUST NOT be consumed by the simulator (D-79)."
        )
        self.path = path


class SnapshotValidationError(BithumbBotError):
    """Raised when a snapshot's structure is invalid per D-75 / D-80.

    Distinct from ``SidecarHashMismatchError`` — the sidecar was fine but
    the parsed model fails a required-content invariant (missing schema
    version, missing required ``verification_status`` key per D-83, etc.).
    """


class AuthConstructionError(BithumbBotError):
    """Raised when JWT construction fails (D-70 / T-1-04-01).

    The exception message is fixed to ``see structured logs`` — the
    original error text and any credential material are deliberately
    NOT spliced in. Structured logs (with ``redact_secrets`` in the
    processor chain) carry any diagnostic detail with secrets masked.
    """

    def __init__(self) -> None:
        super().__init__("Auth construction failed — see structured logs")


class MissingIntervalInStopWindowError(BithumbBotError):
    """Raised when a candle-interval slot between stop activation and the
    evaluation cursor is not confirmed present.

    Refused whether the slot is:

    * listed in ``CandleDataset.missing_intervals_utc`` (reported gap
      — the data pipeline knows the candle is missing), OR
    * simply absent from ``CandleDataset.candles`` without a listing
      (unreported gap — an inconsistency in the dataset itself).

    Either way the evaluator cannot rule out that the stop should have
    already triggered inside the missing window, so a trigger decision
    would be dishonest. The safe answer is refuse and let the caller
    fetch a complete dataset.
    """


class NoNextCandleError(BithumbBotError):
    """Raised when :func:`execute_intent` cannot find a fill candle.

    The engine looks for the earliest candle in the dataset whose
    ``open_time_utc >= intent.signal_ts_utc``. If the dataset ends
    before that boundary — or the signal fires on the last candle
    and no later one exists — the fill is impossible without
    fabricating a price, which the pipeline never does.
    """


class InsufficientCashError(BithumbBotError):
    """Raised when a buy's ``total_cash_debit_krw`` exceeds available cash.

    Fee-on-top model: the check is ``order_notional + fee <=
    ledger.cash_krw``. Never partial-fills. The strategy must size
    down or wait.
    """


class InsufficientPositionError(BithumbBotError):
    """Raised when a sell's filled qty exceeds the current position."""


class BelowMinimumOrderError(BithumbBotError):
    """Raised when the fill would violate the venue's per-side minimum.

    Buy path: ``order_notional_krw < snapshot.minimums.krw_min_total_bid``.
    Sell path: ``filled_qty < snapshot.minimums.krw_min_total_ask``
    (Bithumb's ``ask.min_total`` is a coin-quantity minimum for asks).
    Also raised when qty-step flooring reduces the order to zero.
    """


class NotionalCapExceededError(BithumbBotError):
    """Raised when the pre-fee notional exceeds ``max_notional_krw`` (D-09).

    Applied to ``order_notional_krw`` on buys and ``gross_proceeds_krw``
    on sells — the notional the slippage model is applied to. The cap
    itself is sourced from ``max_validated_notional_krw`` once frozen
    at Gate 2, or from the interim ``provisional_engineering_notional_krw``
    before then. This engine never re-derives the cap — the caller
    passes it in.
    """


class UnverifiedFeeModelError(BithumbBotError):
    """Raised when the snapshot's fee-model verification status is
    inadequate for the requested side.

    * ``confirmed_read_only``    — engine proceeds.
    * ``provisional_documented`` — engine proceeds ONLY when the caller
      explicitly opts in via ``ExecutionConfig.allow_provisional_fee_model
      = True``. Strategy evaluation must NOT opt in; only engine tests
      with a conservative fixture may.
    * ``unresolved_until_M6B`` / ``contradicted`` / absent — hard
      refused; opt-in has no effect.
    """


class CandleValidationError(BithumbBotError):
    """Raised when a candle row or page fails structural validation.

    Covers impossible OHLC (``low > open|close`` or ``high < open|close``),
    non-monotonic server order within a page, cross-boundary duplicates
    whose OHLCV values conflict, wrong ``market`` or ``unit`` fields, and
    malformed timestamps. The first failing row is named in the message
    so an operator can locate it in the raw page. No fabrication path
    exists — missing intervals are REPORTED separately, never synthesized.
    """


class PublicRestNotVerifiedError(BithumbBotError):
    """Raised when a real network fetch is attempted before the public
    REST rate-limit configuration is a frozen M1 verification item.

    The public-REST TokenBucket in :mod:`bithumb_bot.bithumb_spec.
    rate_limits` currently ships a documented TEST-ONLY sentinel
    (Open Verification Item #3). Real network use is fail-closed until
    a verified capacity/refill_rate is frozen. Tests bypass this guard
    by injecting ``httpx.MockTransport`` — the guard fires only when
    the caller lets ``transport`` default to ``None``.
    """


class UnresolvedFactError(BithumbBotError):
    """Raised when a `VERIFICATION.md` bundle contains an unresolved fact.

    D-78: every required build-time fact MUST reach status ``confirmed``
    or ``contradicted`` before ``bt m1 verify-facts`` advances. A fact
    still marked ``unresolved`` (or unchecked entirely) fails this
    check with the fact name.
    """

    def __init__(self, fact_name: str) -> None:
        super().__init__(
            f"VERIFICATION.md fact {fact_name!r} is still unresolved. "
            "Every required build-time fact must be human-approved as "
            "'confirmed' or 'contradicted' before this bundle can advance "
            "(D-78 / D-84)."
        )
        self.fact_name = fact_name


__all__ = [
    "AmbiguousSecretsConfigurationError",
    "AuthConstructionError",
    "BelowMinimumOrderError",
    "BithumbBotError",
    "CandleValidationError",
    "CriticalCorruptionAlert",
    "Gate1LoadError",
    "InsufficientCashError",
    "InsufficientPositionError",
    "MissingIntervalInStopWindowError",
    "NoNextCandleError",
    "NotionalCapExceededError",
    "ProhibitedCredentialDetectedError",
    "PublicRestNotVerifiedError",
    "SecretsFileInsideRepoError",
    "SidecarHashMismatchError",
    "SnapshotAlreadyConsumedError",
    "SnapshotValidationError",
    "UnknownCapabilityError",
    "UnresolvedFactError",
    "UnverifiedFeeModelError",
]
