"""Ephemeral trade-credential loader for the ``bt live breakout-cycle`` capability.

This is the SOLE loader that returns the trade-permission credential class.
Every other capability continues to refuse trade credentials via the
validator's ``_check_trade_credential_prohibition`` path.

Discipline:

* Trade credentials are loaded via the existing
  :func:`bithumb_bot.secrets.loader.load_secrets` machinery — env vars +
  optional external secrets file with the same D-65/D-66 rules.
* Values are returned as plain ``str`` inside a
  :class:`LoadedTradeCredentials` dataclass whose ``__repr__`` masks the
  value (D-70). Callers MUST let the returned instance go out of scope
  after the ephemeral use.
* Missing / partial credentials fail closed with
  :class:`TradeCredentialsMissingError` BEFORE any network or filesystem
  mutation.
* No withdrawal credential exists in this project (D-69) — this module
  intentionally has zero withdrawal-adjacent fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bithumb_bot.errors import BithumbBotError
from bithumb_bot.secrets.loader import load_secrets


class TradeCredentialsMissingError(BithumbBotError):
    """Raised when either trade credential field is absent.

    Only the fact of absence is reported — never the presence-or-value
    of the account/read pair, nor any credential value. Caller catches
    this to render a clean CLI refusal.
    """


@dataclass
class LoadedTradeCredentials:
    """Trade access/secret pair held ephemerally by the caller.

    ``__repr__`` masks the secret material so a stray ``print(creds)``
    or exception format cannot leak values. Callers still MUST NOT log
    or persist the raw ``access_key`` / ``secret_key`` attributes.
    """

    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __repr__(self) -> str:  # pragma: no cover — masking
        return "LoadedTradeCredentials(access_key=***, secret_key=***)"


def load_trade_credentials(repo_root: Path) -> LoadedTradeCredentials:
    """Load the trade credential class ephemerally.

    Reuses :func:`load_secrets` for env / external-file discovery and
    D-65/D-66 enforcement. Raises
    :class:`TradeCredentialsMissingError` if either field is unset —
    the presence check is a boolean on ``settings.trade_*`` fields; no
    value is inspected.

    Args:
        repo_root: Repository root path used by ``load_secrets`` for
            symlink-safe in-repo rejection of an external secrets file.

    Returns:
        A fresh :class:`LoadedTradeCredentials`. Caller MUST let the
        instance go out of scope (D-89) after the operation completes.

    Raises:
        TradeCredentialsMissingError: either field is unset.
    """
    settings = load_secrets(repo_root)
    if (
        settings.trade_access_key is None
        or settings.trade_secret_key is None
    ):
        raise TradeCredentialsMissingError(
            "trade credential class is required for 'bt live breakout-cycle "
            "--mode live' but neither the OS environment nor the external "
            "secrets file (BITHUMB_BOT_SECRETS_FILE) supplied BOTH "
            "trade_access_key and trade_secret_key (values not inspected)."
        )
    return LoadedTradeCredentials(
        access_key=settings.trade_access_key.get_secret_value(),
        secret_key=settings.trade_secret_key.get_secret_value(),
    )


__all__ = [
    "LoadedTradeCredentials",
    "TradeCredentialsMissingError",
    "load_trade_credentials",
]
