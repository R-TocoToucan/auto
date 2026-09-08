"""Integration smoke composing plans 01-01 + 01-02.

1. `load_secrets(repo_root)` succeeds when no trade env var is set.
2. `validate(("m1","fetch-spec"))` returns ok=True when the account/read
   credentials are set to sentinel non-empty values via monkeypatch.
3. Adding `BITHUMB_TRADE_ACCESS_KEY` env → `validate(...)` returns
   ok=False with the specific `trade_credential_prohibited` reason;
   the reason string contains NO substring of the credential value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bithumb_bot.config.validator import REPO_ROOT_ENV, validate
from bithumb_bot.secrets.loader import SECRETS_FILE_ENV, load_secrets

_ALL_CRED_ENV = (
    "BITHUMB_ACCOUNT_READ_ACCESS_KEY",
    "BITHUMB_ACCOUNT_READ_SECRET_KEY",
    "BITHUMB_TRADE_ACCESS_KEY",
    "BITHUMB_TRADE_SECRET_KEY",
)

_LEAK_SENTINEL = "sentinel_leak_XYZ_never_appears_in_reason"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (SECRETS_FILE_ENV, *_ALL_CRED_ENV):
        monkeypatch.delenv(name, raising=False)


def test_load_secrets_succeeds_when_no_trade_env(
    tmp_path: Path,
) -> None:
    settings = load_secrets(tmp_path)
    assert settings.account_read_access_key is None
    assert settings.trade_access_key is None


def test_validate_m1_fetch_spec_ok_with_account_read_creds(
    tmp_gate1_toml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_gate1_toml.parent.parent.parent
    monkeypatch.setenv(REPO_ROOT_ENV, str(repo_root))
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "account-sentinel")
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "account-secret")
    result = validate(("m1", "fetch-spec"))
    assert result.ok is True, f"expected ok=True, got {result!r}"
    assert result.missing == ()


def test_validate_refuses_when_trade_cred_present(
    tmp_gate1_toml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_gate1_toml.parent.parent.parent
    monkeypatch.setenv(REPO_ROOT_ENV, str(repo_root))
    monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", _LEAK_SENTINEL)
    result = validate(("m0", "selfcheck"))
    assert result.ok is False
    assert result.missing == ("trade_credential_prohibited",)
    assert result.reason is not None
    # D-70: no substring of the sentinel value appears anywhere in the
    # ValidationResult surface (reason, missing tuple, or repr).
    assert _LEAK_SENTINEL not in result.reason
    assert _LEAK_SENTINEL not in str(result.missing)
    assert _LEAK_SENTINEL not in repr(result)


def test_validate_m1_fetch_spec_refuses_when_trade_cred_present(
    tmp_gate1_toml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_gate1_toml.parent.parent.parent
    monkeypatch.setenv(REPO_ROOT_ENV, str(repo_root))
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "account-sentinel")
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "account-secret")
    monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", _LEAK_SENTINEL)
    result = validate(("m1", "fetch-spec"))
    # Trade-cred prohibition is checked before account-read cred
    # (short-circuit on the security-critical fail).
    assert result.ok is False
    assert result.missing == ("trade_credential_prohibited",)
    assert result.reason is not None
    assert _LEAK_SENTINEL not in result.reason
