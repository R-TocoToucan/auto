"""Secrets bootstrap for `bithumb_bot`.

Two public functions:

* :func:`load_secrets` — construct a fresh :class:`~bithumb_bot.secrets.
  settings.BithumbSecrets` from the OS environment, optionally augmented
  by an external `KEY=value` secrets file selected via the
  ``BITHUMB_BOT_SECRETS_FILE`` env var. Enforces:

  * **D-65 (symlink-safe in-repo rejection)** — the resolved (real)
    target of the secrets file MUST NOT live under the resolved (real)
    repository root. Rejection raises
    :class:`~bithumb_bot.errors.SecretsFileInsideRepoError`.
  * **D-66 rule 3 (ambiguous mixed sources)** — a credential key
    defined in both `os.environ` and the file with **different** values
    raises :class:`~bithumb_bot.errors.AmbiguousSecretsConfiguration
    Error` (env-wins for same-value overlap; env-wins when the file is
    silent on a key).

* :func:`reject_trade_credentials` — refuse pre-M6B trade credentials
  (D-68 / D-97). Raises
  :class:`~bithumb_bot.errors.ProhibitedCredentialDetectedError` with
  ``credential_class="trade"`` on any non-``None`` trade field. **D-70**:
  the raised exception's ``str()`` contains only the credential *class*
  string — never any part of the value.

**No withdrawal path exists (D-69)** — this module has zero
withdrawal-adjacent branches, fields, or comment references beyond this
citation.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import SecretStr

from bithumb_bot.errors import (
    AmbiguousSecretsConfigurationError,
    ProhibitedCredentialDetectedError,
    SecretsFileInsideRepoError,
)
from bithumb_bot.secrets.settings import BithumbSecrets

#: Env var pointing at the external secrets file (D-65).
SECRETS_FILE_ENV = "BITHUMB_BOT_SECRETS_FILE"

#: Every credential env-var name (D-67 key set — the ones the file / env
#: precedence comparison scans).
_ALL_CREDENTIAL_ENV_KEYS: tuple[str, ...] = (
    "BITHUMB_ACCOUNT_READ_ACCESS_KEY",
    "BITHUMB_ACCOUNT_READ_SECRET_KEY",
    "BITHUMB_TRADE_ACCESS_KEY",
    "BITHUMB_TRADE_SECRET_KEY",
)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=value`` file.

    Rules (deliberately narrow — this is a *secrets* file, not a shell):

    * Each non-blank, non-comment line is split on the FIRST ``=``.
    * Leading and trailing whitespace on both sides is stripped.
    * No shell substitution, no ``export`` prefix handling, no quoting
      nuance — the value is taken literally.
    * ``#``-prefixed lines and blank lines are ignored.
    * Duplicate keys: last wins.
    """
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            # A line without `=` is malformed; skip silently rather than
            # raising a value-adjacent error that could leak content.
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _resolve_secrets_file(raw_path: str, repo_root: Path) -> Path:
    """Resolve the secrets-file path and reject any in-repo target (D-65).

    Uses ``Path.resolve(strict=True)`` on BOTH the secrets path and the
    repo root — this follows every symlink to its real target so a
    symlink whose target sits under the repo cannot slip past the check.

    Raises ``FileNotFoundError`` if the path does not exist
    (strict=True), and ``SecretsFileInsideRepoError`` if the resolved
    target is inside the resolved repo root.
    """
    resolved_secrets = Path(raw_path).resolve(strict=True)
    resolved_repo = repo_root.resolve(strict=True)
    if resolved_secrets == resolved_repo or resolved_secrets.is_relative_to(
        resolved_repo
    ):
        raise SecretsFileInsideRepoError(resolved_secrets)
    return resolved_secrets


def _check_ambiguous_overlap(
    file_contents: dict[str, str], env: os._Environ[str] | dict[str, str]
) -> None:
    """Raise if any tracked credential key has different values in env and file.

    Only inspects the D-67 credential keys — arbitrary unrelated file
    entries are ignored.
    """
    for key in _ALL_CREDENTIAL_ENV_KEYS:
        env_value = env.get(key)
        file_value = file_contents.get(key)
        if env_value is not None and file_value is not None and env_value != file_value:
            raise AmbiguousSecretsConfigurationError(key)


def load_secrets(repo_root: Path) -> BithumbSecrets:
    """Load a fresh `BithumbSecrets` (env only, or env + external file).

    Args:
        repo_root: Absolute path to the repository root. Used only to
            verify the external secrets file (if any) lives OUTSIDE the
            repo tree (D-65). Symlinks are followed on both sides before
            the containment check.

    Returns:
        A freshly-constructed ``BithumbSecrets``. Per D-89 the caller
        MUST let this return value go out of scope after use — never
        cache it in a module-global.

    Raises:
        SecretsFileInsideRepoError: `BITHUMB_BOT_SECRETS_FILE` resolves
            inside `repo_root` (D-65).
        AmbiguousSecretsConfigurationError: same credential key appears
            in both `os.environ` and the file with different values
            (D-66 rule 3).
        FileNotFoundError: `BITHUMB_BOT_SECRETS_FILE` is set but points
            at a non-existent path (strict resolve).
    """
    raw_file_env = os.environ.get(SECRETS_FILE_ENV)
    if raw_file_env is None:
        # Env only — pydantic-settings reads BITHUMB_* directly from os.environ.
        return BithumbSecrets()

    resolved_file = _resolve_secrets_file(raw_file_env, repo_root)
    file_contents = _parse_env_file(resolved_file)

    # D-66 rule 3 — reject differing overlaps BEFORE constructing anything.
    _check_ambiguous_overlap(file_contents, os.environ)

    # Env-only base — pydantic-settings pulls every BITHUMB_* directly from
    # os.environ. This is the "env wins" side of D-66.
    settings = BithumbSecrets()

    # Merge: overlay file-only keys onto env-loaded settings. `model_copy`
    # is used so we do not fight pydantic-settings' generated `__init__`
    # signature (which reserves many keyword-only positions for internal
    # source selectors).
    overlay: dict[str, SecretStr] = {}
    for env_key in _ALL_CREDENTIAL_ENV_KEYS:
        if env_key in os.environ:
            continue  # env wins — already loaded above
        file_value = file_contents.get(env_key)
        if file_value is not None:
            overlay[_env_key_to_field(env_key)] = SecretStr(file_value)
    if overlay:
        settings = settings.model_copy(update=overlay)
    return settings


def _env_key_to_field(env_key: str) -> str:
    """Translate a ``BITHUMB_*`` env-var name to its `BithumbSecrets` field.

    e.g. ``BITHUMB_ACCOUNT_READ_ACCESS_KEY`` → ``account_read_access_key``.
    """
    if not env_key.startswith("BITHUMB_"):
        raise ValueError(
            f"env key {env_key!r} does not use the required BITHUMB_ prefix"
        )
    return env_key[len("BITHUMB_") :].lower()


def reject_trade_credentials(settings: BithumbSecrets) -> None:
    """Raise if either trade credential field is present (D-68 / D-97).

    Reads only the fact of presence — never the credential value (D-70).
    The raised exception's message contains only the credential class
    string ``'trade'``.

    Args:
        settings: A freshly-constructed ``BithumbSecrets``. Caller is
            responsible for ephemeral scope (D-89).

    Raises:
        ProhibitedCredentialDetectedError: either `trade_access_key`
            or `trade_secret_key` is not None.
    """
    # Boolean coercion via `is not None` — do NOT unwrap the SecretStr.
    if settings.trade_access_key is not None or settings.trade_secret_key is not None:
        raise ProhibitedCredentialDetectedError(credential_class="trade")


__all__ = [
    "SECRETS_FILE_ENV",
    "load_secrets",
    "reject_trade_credentials",
]
