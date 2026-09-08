"""Tests for `bithumb_bot.secrets.loader` — secrets bootstrap.

Covers task 01-02-05 Behavior contract:

* `load_secrets(repo_root)` uses `Path.resolve(strict=True)` +
  `is_relative_to(repo_root.resolve(strict=True))` to reject any
  `BITHUMB_BOT_SECRETS_FILE` that resolves inside the repo tree
  (D-65 — symlink-safe).
* Absolute in-repo paths and symlink-to-in-repo paths are BOTH rejected.
* Env-vs-file same-key different-value → `AmbiguousSecretsConfigurationError`
  (D-66 rule 3). Env-wins on same-value overlap; env-wins when the file
  is absent for a key.
* `reject_trade_credentials(settings)` raises
  `ProhibitedCredentialDetectedError` on any non-None trade field; the
  exception's `str()` contains only `trade` — never any part of the
  sentinel value (D-70).
* The validator hook: with a trade env var set, `validate((verb, sv))`
  for a pre-M6B capability returns `ok=False`, `missing=
  ("trade_credential_prohibited",)`, and the reason has no substring of
  the credential value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from bithumb_bot.errors import (
    AmbiguousSecretsConfigurationError,
    ProhibitedCredentialDetectedError,
    SecretsFileInsideRepoError,
)
from bithumb_bot.secrets.loader import (
    SECRETS_FILE_ENV,
    load_secrets,
    reject_trade_credentials,
)
from bithumb_bot.secrets.settings import BithumbSecrets

_ALL_CRED_ENV = (
    "BITHUMB_ACCOUNT_READ_ACCESS_KEY",
    "BITHUMB_ACCOUNT_READ_SECRET_KEY",
    "BITHUMB_TRADE_ACCESS_KEY",
    "BITHUMB_TRADE_SECRET_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every credential + BITHUMB_BOT_SECRETS_FILE env var per test."""
    for name in (SECRETS_FILE_ENV, *_ALL_CRED_ENV):
        monkeypatch.delenv(name, raising=False)


def _write_env_file(path: Path, mapping: dict[str, str]) -> None:
    """Write a simple `KEY=value` file (no shell semantics)."""
    lines = [f"{k}={v}" for k, v in mapping.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Path safety (D-65)
# ---------------------------------------------------------------------------


class TestSecretsFilePathSafety:
    def test_absolute_in_repo_path_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # tmp_path acts as the repo root; file is directly inside it.
        secrets_file = tmp_path / "sub" / "secrets.env"
        secrets_file.parent.mkdir(parents=True)
        _write_env_file(secrets_file, {"BITHUMB_ACCOUNT_READ_ACCESS_KEY": "v"})
        monkeypatch.setenv(SECRETS_FILE_ENV, str(secrets_file))
        with pytest.raises(SecretsFileInsideRepoError) as excinfo:
            load_secrets(tmp_path)
        # The exception carries the resolved path; assert it names the file.
        assert "secrets.env" in str(excinfo.value)

    @pytest.mark.skipif(
        sys.platform == "win32" and not os.environ.get("CI"),
        reason="Symlink creation on Windows requires developer mode / admin.",
    )
    def test_symlink_into_repo_rejected(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `repo_root` and `outside_dir` are siblings so a symlink from
        # outside → inside genuinely crosses the boundary.
        repo_root = tmp_path
        outside_dir = tmp_path_factory.mktemp("external_secrets_source")
        real_secret = repo_root / "real_inside.env"
        _write_env_file(real_secret, {"BITHUMB_ACCOUNT_READ_ACCESS_KEY": "v"})
        symlink = outside_dir / "looks_outside.env"
        try:
            symlink.symlink_to(real_secret)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
        monkeypatch.setenv(SECRETS_FILE_ENV, str(symlink))
        with pytest.raises(SecretsFileInsideRepoError):
            load_secrets(repo_root)

    def test_outside_path_accepted(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo_root = tmp_path
        outside_dir = tmp_path_factory.mktemp("truly_external_secrets")
        secrets_file = outside_dir / "secrets.env"
        _write_env_file(
            secrets_file, {"BITHUMB_ACCOUNT_READ_ACCESS_KEY": "outside-value"}
        )
        monkeypatch.setenv(SECRETS_FILE_ENV, str(secrets_file))
        settings = load_secrets(repo_root)
        assert settings.account_read_access_key is not None
        assert (
            settings.account_read_access_key.get_secret_value() == "outside-value"
        )

    def test_no_secrets_file_env_returns_env_only_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "env-only-value")
        settings = load_secrets(tmp_path)
        assert settings.account_read_access_key is not None
        assert (
            settings.account_read_access_key.get_secret_value() == "env-only-value"
        )


# ---------------------------------------------------------------------------
# Ambiguous mixed configuration (D-66 rule 3)
# ---------------------------------------------------------------------------


class TestAmbiguousMixed:
    def test_env_and_file_disagree_raises(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo_root = tmp_path
        outside_dir = tmp_path_factory.mktemp("external_ambiguous")
        secrets_file = outside_dir / "secrets.env"
        _write_env_file(
            secrets_file, {"BITHUMB_ACCOUNT_READ_ACCESS_KEY": "value-from-file"}
        )
        monkeypatch.setenv(SECRETS_FILE_ENV, str(secrets_file))
        monkeypatch.setenv(
            "BITHUMB_ACCOUNT_READ_ACCESS_KEY", "different-value-from-env"
        )
        with pytest.raises(AmbiguousSecretsConfigurationError) as excinfo:
            load_secrets(repo_root)
        assert "BITHUMB_ACCOUNT_READ_ACCESS_KEY" in str(excinfo.value)
        # Values MUST NEVER appear in the exception text.
        assert "value-from-file" not in str(excinfo.value)
        assert "different-value-from-env" not in str(excinfo.value)

    def test_env_and_file_same_value_no_error(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo_root = tmp_path
        outside_dir = tmp_path_factory.mktemp("external_agree")
        secrets_file = outside_dir / "secrets.env"
        _write_env_file(
            secrets_file, {"BITHUMB_ACCOUNT_READ_ACCESS_KEY": "same-value"}
        )
        monkeypatch.setenv(SECRETS_FILE_ENV, str(secrets_file))
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "same-value")
        settings = load_secrets(repo_root)
        assert settings.account_read_access_key is not None
        assert (
            settings.account_read_access_key.get_secret_value() == "same-value"
        )

    def test_env_wins_when_file_absent_for_key(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo_root = tmp_path
        outside_dir = tmp_path_factory.mktemp("external_partial")
        secrets_file = outside_dir / "secrets.env"
        _write_env_file(
            secrets_file, {"UNRELATED_KEY_A": "x", "UNRELATED_KEY_B": "y"}
        )
        monkeypatch.setenv(SECRETS_FILE_ENV, str(secrets_file))
        monkeypatch.setenv(
            "BITHUMB_ACCOUNT_READ_ACCESS_KEY", "env-only-provided"
        )
        settings = load_secrets(repo_root)
        assert settings.account_read_access_key is not None
        assert (
            settings.account_read_access_key.get_secret_value()
            == "env-only-provided"
        )


# ---------------------------------------------------------------------------
# reject_trade_credentials (D-68, D-70, D-97)
# ---------------------------------------------------------------------------


_HEX_SENTINEL = "0xdeadbeef01234567890abcdef_leak_sentinel_XYZ"


class TestRejectTradeCredentials:
    def test_raises_when_trade_access_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", _HEX_SENTINEL)
        settings = BithumbSecrets()
        with pytest.raises(ProhibitedCredentialDetectedError) as excinfo:
            reject_trade_credentials(settings)
        assert excinfo.value.credential_class == "trade"
        # D-70: value MUST NOT leak.
        message = str(excinfo.value)
        assert "trade" in message
        assert _HEX_SENTINEL not in message
        assert "deadbeef" not in message
        assert "0xdead" not in message

    def test_raises_when_trade_secret_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_TRADE_SECRET_KEY", _HEX_SENTINEL)
        settings = BithumbSecrets()
        with pytest.raises(ProhibitedCredentialDetectedError):
            reject_trade_credentials(settings)

    def test_passes_when_only_account_read_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "ok")
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "ok")
        settings = BithumbSecrets()
        # Must not raise.
        reject_trade_credentials(settings)

    def test_passes_when_no_creds(self) -> None:
        settings = BithumbSecrets()
        reject_trade_credentials(settings)


# ---------------------------------------------------------------------------
# Validator wiring (task 01-02-05 hook into 01-01's validate())
# ---------------------------------------------------------------------------


class TestValidatorTradeCredHook:
    """`validate()` should return ok=False instead of raising when a trade
    env var is set — per task 01-02-05 description: "return
    ValidationResult(ok=False, missing=('trade_credential_prohibited',),
    reason='...') instead of raising, so the CLI dispatcher prints a
    clean refusal."
    """

    def test_validate_returns_ok_false_on_trade_env(
        self,
        tmp_gate1_toml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from bithumb_bot.config.validator import REPO_ROOT_ENV, validate

        repo_root = tmp_gate1_toml.parent.parent.parent
        monkeypatch.setenv(REPO_ROOT_ENV, str(repo_root))
        monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", _HEX_SENTINEL)
        result = validate(("m0", "selfcheck"))
        assert result.ok is False
        assert result.missing == ("trade_credential_prohibited",)
        assert result.reason is not None
        # D-70: no substring of the sentinel value appears in the reason.
        assert _HEX_SENTINEL not in result.reason
        assert "deadbeef" not in result.reason
        assert "trade" in result.reason.lower()
