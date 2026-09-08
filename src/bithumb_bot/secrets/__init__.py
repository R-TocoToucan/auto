"""Secrets loading + redaction for `bithumb_bot`.

Public surface (per plan 01-02):

* ``BithumbSecrets`` — pydantic-settings model with exactly four credential
  fields (`BITHUMB_ACCOUNT_READ_*`, `BITHUMB_TRADE_*`) per D-67. See
  ``bithumb_bot.secrets.settings``.
* ``load_secrets`` — bootstrap that enforces the D-65 in-repo-path
  rejection (symlink-safe) and the D-66 ambiguous-mixed-source rejection.
  See ``bithumb_bot.secrets.loader``.
* ``reject_trade_credentials`` — pre-M6B trade-cred rejection (D-68 /
  D-97). Raises ``ProhibitedCredentialDetectedError`` with class-only
  reporting (D-70). See ``bithumb_bot.secrets.loader``.
* ``redact_secrets`` — structlog processor that masks ``SecretStr``
  values in the event dict. See ``bithumb_bot.secrets.redaction``.

**D-89**: ``BithumbSecrets()`` MUST be constructed ephemerally — never
held as a module-global. Every capability that needs a credential
constructs a fresh instance inside the exact operation that needs it,
and lets it go out of scope immediately after.

Submodules are imported lazily (via explicit `from bithumb_bot.secrets
import X` at the call site) rather than eagerly re-exported here — this
keeps 01-04's structlog wiring from paying an import-time cost when it
only needs the `redaction` module.
"""
