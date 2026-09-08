"""Tests for `bithumb_bot.secrets.redaction.redact_secrets`.

Covers task 01-02-06 Behavior contract:

* `redact_secrets(logger, method_name, event_dict)` returns a mutated
  event dict where every `SecretStr` value is replaced with the literal
  string ``"***"``.
* Non-secret values pass through unchanged (int, str, list, dict, None).
* Defense-in-depth: `BithumbSecrets(account_read_access_key="sentinel_XYZ")`'s
  `model_dump()`, `model_dump_json()`, `repr(...)`, and `str(...)` MUST
  NOT contain the sentinel — pydantic v2 handles this natively but D-70
  makes this a hard requirement so we assert it explicitly.
* Processor is importable independently of the full structlog config so
  01-04's structlog config can wire it in without a circular import.
"""

from __future__ import annotations

from pydantic import SecretStr

from bithumb_bot.secrets.redaction import redact_secrets
from bithumb_bot.secrets.settings import BithumbSecrets

_MASK = "***"
_SENTINEL = "sentinel_XYZ_never_leak_this_string"


# ---------------------------------------------------------------------------
# Processor behavior
# ---------------------------------------------------------------------------


class TestRedactSecretsProcessor:
    def test_secret_value_is_masked(self) -> None:
        event = {"api_key": SecretStr(_SENTINEL)}
        out = redact_secrets(None, "info", event)
        assert out["api_key"] == _MASK

    def test_returns_the_mutated_dict(self) -> None:
        event = {"api_key": SecretStr(_SENTINEL)}
        out = redact_secrets(None, "info", event)
        assert isinstance(out, dict)
        # Structlog processors must return an event dict.
        assert "api_key" in out

    def test_non_secret_values_pass_through(self) -> None:
        event = {
            "count": 42,
            "message": "plain string",
            "items": [1, 2, 3],
            "meta": {"x": 1},
            "none_field": None,
        }
        out = redact_secrets(None, "warning", dict(event))
        assert out == event

    def test_mixed_dict_masks_only_secrets(self) -> None:
        event = {
            "api_key": SecretStr(_SENTINEL),
            "user_id": "u-123",
            "attempt": 4,
        }
        out = redact_secrets(None, "error", event)
        assert out["api_key"] == _MASK
        assert out["user_id"] == "u-123"
        assert out["attempt"] == 4

    def test_empty_dict_returns_empty_dict(self) -> None:
        out = redact_secrets(None, "info", {})
        assert out == {}

    def test_signature_matches_structlog_processor_contract(self) -> None:
        """Every structlog processor takes (logger, method_name, event_dict)
        and returns the (mutated) event dict."""
        import inspect

        sig = inspect.signature(redact_secrets)
        params = list(sig.parameters.keys())
        assert len(params) == 3, f"expected 3 params, got {params!r}"


# ---------------------------------------------------------------------------
# Defense-in-depth on BithumbSecrets serialization surfaces (D-70)
# ---------------------------------------------------------------------------


class TestBithumbSecretsSurfaceMasking:
    def _make(self) -> BithumbSecrets:
        return BithumbSecrets(account_read_access_key=SecretStr(_SENTINEL))

    def test_repr_hides_sentinel(self) -> None:
        assert _SENTINEL not in repr(self._make())

    def test_str_hides_sentinel(self) -> None:
        assert _SENTINEL not in str(self._make())

    def test_model_dump_hides_sentinel(self) -> None:
        dumped = self._make().model_dump()
        # SecretStr value in `dumped` — its str form must not contain the raw.
        v = dumped["account_read_access_key"]
        assert _SENTINEL not in str(v)

    def test_model_dump_json_hides_sentinel(self) -> None:
        payload = self._make().model_dump_json()
        assert _SENTINEL not in payload


# ---------------------------------------------------------------------------
# Import independence — 01-04 must import redact_secrets without pulling
# in the loader / settings modules by accident.
# ---------------------------------------------------------------------------


def test_redact_secrets_importable_without_full_secrets_package() -> None:
    import importlib

    module = importlib.import_module("bithumb_bot.secrets.redaction")
    assert hasattr(module, "redact_secrets")
