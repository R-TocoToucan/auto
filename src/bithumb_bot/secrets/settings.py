"""`BithumbSecrets` — pydantic-settings model for the four D-67 credentials.

Exactly two credential classes exist for this project (D-67):

* **account/read** — read-only, JWT-authenticated, used by M1 spec-fetch
  and any future read-only account operation.
* **trade**        — trade-permission, **prohibited pre-M6B** (D-68 /
  D-97). This model declares the fields so the loader (`loader.py`)
  can positively detect their presence and refuse. The refusal path
  never inspects the value (D-70).

**No withdrawal credential exists anywhere in this project (D-69)**.
Withdrawal permission is permanently disabled on every API key ever
configured for `bithumb_bot`, per the project's invariant safety rules.
This module intentionally has zero `withdrawal_*` fields and any commit
that adds one is a hard failure (see
`tests/secrets/test_no_withdrawal_credential.py`).

Every field is typed `Optional[SecretStr]` and defaults to `None`. This
lets `bithumb_bot.secrets.loader.load_secrets` distinguish "unset" from
"present-but-empty" — both are refused by any capability that requires a
credential, but they emit different failure reasons.

**D-89**: `BithumbSecrets()` MUST be constructed **ephemerally**, never
held as a module-global. Every capability that touches a credential
constructs a fresh instance inside the exact operation, then lets it go
out of scope. Ephemeral construction is the only reason `SecretStr`
masking is worth anything — a long-lived global would still leak via
introspection.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BithumbSecrets(BaseSettings):
    """The four D-67 credential fields — every one `Optional[SecretStr]`.

    Env-var mapping (via `env_prefix="BITHUMB_"`, case-insensitive):

        account_read_access_key  ← BITHUMB_ACCOUNT_READ_ACCESS_KEY
        account_read_secret_key  ← BITHUMB_ACCOUNT_READ_SECRET_KEY
        trade_access_key         ← BITHUMB_TRADE_ACCESS_KEY   (prohibited pre-M6B)
        trade_secret_key         ← BITHUMB_TRADE_SECRET_KEY   (prohibited pre-M6B)

    D-64: uses `pydantic_settings.BaseSettings` (NOT the deprecated
    `pydantic.BaseSettings`).

    D-70: `SecretStr` masks the value on `repr()`, `str()`, and JSON
    serialization. The `redact_secrets` structlog processor
    (`bithumb_bot.secrets.redaction`) is a belt-and-suspenders second
    layer for the observability pipeline.

    `extra="forbid"` — an unknown `BITHUMB_*` env var (or kwarg) raises
    `ValidationError` instead of silently dropping the value. This
    catches typos that would otherwise leave a credential un-loaded.
    """

    model_config = SettingsConfigDict(
        env_prefix="BITHUMB_",
        case_sensitive=False,
        secrets_dir=None,
        extra="forbid",
    )

    # Field order mirrors D-67's credential-class ordering; every field is
    # `Optional[SecretStr]` so unset env yields `None`.
    account_read_access_key: SecretStr | None = None
    account_read_secret_key: SecretStr | None = None
    trade_access_key: SecretStr | None = None
    trade_secret_key: SecretStr | None = None


__all__ = ["BithumbSecrets"]
