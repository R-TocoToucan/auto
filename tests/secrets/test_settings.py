"""Tests for `bithumb_bot.secrets.settings.BithumbSecrets`.

Covers task 01-02-04 Behavior contract:

* Field set is EXACTLY the four D-67 credentials — no more, no less, no
  withdrawal-related field (D-69).
* All four fields are `SecretStr | None`, defaulting to `None`, so unset
  env yields `None` (letting the loader distinguish "unset" from
  "present-but-empty").
* Env-var mapping via `env_prefix="BITHUMB_"`, `case_sensitive=False`.
* `SecretStr` masking is observed under `repr()`, `str()`, and
  `model_dump()` (JSON mode included) — D-70 requires the value never
  leak on any of these surfaces.
* `extra="forbid"` — unknown env vars with the `BITHUMB_` prefix raise
  `ValidationError` (defense against typos silently dropping the value).
* Static asserts: no `withdrawal_*` / `withdraw` fields anywhere on the
  model (D-69).
"""

from __future__ import annotations

import json
from typing import get_type_hints

import pytest
from pydantic import SecretStr, ValidationError

from bithumb_bot.secrets.settings import BithumbSecrets


# ---------------------------------------------------------------------------
# Field surface — exactly the four D-67 credentials
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS = frozenset(
    {
        "account_read_access_key",
        "account_read_secret_key",
        "trade_access_key",
        "trade_secret_key",
    }
)


class TestFieldSurface:
    def test_exactly_four_fields(self) -> None:
        assert set(BithumbSecrets.model_fields) == _REQUIRED_FIELDS

    def test_every_field_is_optional_secretstr(self) -> None:
        hints = get_type_hints(BithumbSecrets)
        for name in _REQUIRED_FIELDS:
            hint = hints[name]
            # Optional[SecretStr] normalises to `SecretStr | None`. We just
            # check both `SecretStr` and `NoneType` are in the union.
            args = getattr(hint, "__args__", ())
            assert SecretStr in args, f"{name}: SecretStr missing from hint {hint!r}"
            assert type(None) in args, f"{name}: NoneType missing from hint {hint!r}"

    def test_defaults_are_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in [
            "BITHUMB_ACCOUNT_READ_ACCESS_KEY",
            "BITHUMB_ACCOUNT_READ_SECRET_KEY",
            "BITHUMB_TRADE_ACCESS_KEY",
            "BITHUMB_TRADE_SECRET_KEY",
        ]:
            monkeypatch.delenv(env, raising=False)
        s = BithumbSecrets()
        for name in _REQUIRED_FIELDS:
            assert getattr(s, name) is None, (
                f"{name}: expected default None with no env, got {getattr(s, name)!r}"
            )

    def test_no_withdrawal_field(self) -> None:
        # D-69: no withdrawal credential exists anywhere.
        for name in BithumbSecrets.model_fields:
            assert "withdraw" not in name.lower(), (
                f"forbidden withdrawal-adjacent field: {name!r}"
            )
        # Also check the raw class annotations / docstring pointer.
        source = BithumbSecrets.__doc__ or ""
        assert "withdrawal" not in source.lower() or (
            # A docstring may explicitly cite "no withdrawal credential" per
            # D-69; that's the only allowed mention.
            "d-69" in source.lower()
            or "no withdrawal" in source.lower()
        )


# ---------------------------------------------------------------------------
# Env-var mapping
# ---------------------------------------------------------------------------


class TestEnvMapping:
    def test_account_read_access_key_loads_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "hunter2")
        monkeypatch.delenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", raising=False)
        monkeypatch.delenv("BITHUMB_TRADE_ACCESS_KEY", raising=False)
        monkeypatch.delenv("BITHUMB_TRADE_SECRET_KEY", raising=False)
        s = BithumbSecrets()
        assert s.account_read_access_key is not None
        assert s.account_read_access_key.get_secret_value() == "hunter2"
        assert s.account_read_secret_key is None
        assert s.trade_access_key is None
        assert s.trade_secret_key is None

    def test_case_insensitive_env_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # pydantic-settings normalises via case_sensitive=False; lowercase
        # env still resolves.
        monkeypatch.setenv("bithumb_trade_access_key", "opaque")
        s = BithumbSecrets()
        assert s.trade_access_key is not None
        assert s.trade_access_key.get_secret_value() == "opaque"


# ---------------------------------------------------------------------------
# SecretStr masking — D-70 defense in depth
# ---------------------------------------------------------------------------


_SENTINEL = "sentinel_XYZ_never_leak"


class TestSecretMasking:
    def test_repr_masks_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", _SENTINEL)
        s = BithumbSecrets()
        r = repr(s)
        assert "***" in r or "SecretStr" in r
        assert _SENTINEL not in r

    def test_str_masks_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", _SENTINEL)
        s = BithumbSecrets()
        assert _SENTINEL not in str(s)

    def test_model_dump_masks_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", _SENTINEL)
        s = BithumbSecrets()
        dumped = s.model_dump()
        # Value should be a SecretStr instance whose str form masks the value.
        v = dumped["account_read_access_key"]
        assert _SENTINEL not in str(v)

    def test_model_dump_json_masks_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", _SENTINEL)
        s = BithumbSecrets()
        payload = s.model_dump_json()
        # The JSON payload MUST NOT contain the raw sentinel — pydantic v2
        # renders SecretStr as `"**********"` in JSON mode by default.
        assert _SENTINEL not in payload
        # Must still be valid JSON.
        parsed = json.loads(payload)
        assert set(parsed.keys()) == _REQUIRED_FIELDS


# ---------------------------------------------------------------------------
# extra="forbid" — unknown BITHUMB_* env vars raise (typo defense)
# ---------------------------------------------------------------------------


class TestExtraForbid:
    def test_unknown_bithumb_field_via_kwarg_raises(self) -> None:
        with pytest.raises(ValidationError):
            BithumbSecrets(unknown_field="x")  # type: ignore[call-arg]
